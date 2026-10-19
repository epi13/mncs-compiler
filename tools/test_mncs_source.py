"""Focused checks for exact MNCS function extraction and evidence publication."""
from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mncs_source import extract_mncs_function, write_json_exclusive


class ExtractFunctionTests(unittest.TestCase):
    def test_extracts_exact_current_compiler_source_definition(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src/compiler/source.mncs").read_bytes()
        body, start, end = extract_mncs_function(source, "span_valid_global")
        self.assertEqual(source[start:end], body)
        self.assertTrue(body.startswith(b"fn span_valid_global("))

    def test_ignores_comment_and_literal_lookalikes_and_nested_braces(self) -> None:
        source = b'''// fn target() { this is not a definition }
/* fn target() { nested /* } */ comment } */
let note: text = "fn target() { also not a definition }";
fn target<T: Nat>(value: T) -> (result: T) {
    // } this brace is in a line comment
    let nested: text = "{ not a block }";
    if true { return value; }
    return value;
}
'''
        body, start, end = extract_mncs_function(source, "target")
        self.assertEqual(source[start:end], body)
        self.assertTrue(body.startswith(b"fn target<T: Nat>"))
        self.assertEqual(body.count(b"if true"), 1)

    def test_duplicate_definitions_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "found 2"):
            extract_mncs_function(b"fn target() {}\nfn target() {}\n", "target")

    def test_missing_and_unterminated_definitions_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "found 0"):
            extract_mncs_function(b"// fn target() {}\n", "target")
        with self.assertRaisesRegex(ValueError, "unterminated body"):
            extract_mncs_function(b"fn target() { if true { return; }", "target")


class WriteJsonExclusiveTests(unittest.TestCase):
    def test_publishes_complete_private_json_and_no_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "proof.json"
            write_json_exclusive(destination, {"status": "passed"})
            self.assertEqual(json.loads(destination.read_text()), {"status": "passed"})
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["proof.json"])

    def test_refuses_existing_file_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "proof.json"
            destination.write_text("newer evidence\n")
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                write_json_exclusive(destination, {"status": "stale"})
            self.assertEqual(destination.read_text(), "newer evidence\n")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["proof.json"])

    def test_failed_atomic_link_leaves_no_published_or_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "proof.json"
            with (
                patch("mncs_source.os.link", side_effect=OSError("link failed")),
                self.assertRaisesRegex(OSError, "link failed"),
            ):
                write_json_exclusive(destination, {"status": "incomplete"})
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_concurrent_publishers_have_one_winner_and_no_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "proof.json"

            def publish(status: str) -> str:
                try:
                    write_json_exclusive(destination, {"status": status})
                    return "published"
                except FileExistsError:
                    return "rejected"

            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(publish, ("first", "second")))
            self.assertEqual(sorted(results), ["published", "rejected"])
            self.assertIn(json.loads(destination.read_text())["status"], {"first", "second"})
            self.assertEqual(len(list(Path(directory).iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
