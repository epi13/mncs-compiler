#!/usr/bin/env python3
"""Focused checks for locked Stage-0 selection in fact-reuse harnesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ensure_locked_stage0_probe import ensure_locked_stage0_probe


class EnsureLockedStage0ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mncs-stage0-lock-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "mncs-compiler"
        self.root.mkdir()
        self.revision = "1" * 40
        (self.root / "tools").mkdir()
        (self.root / "mncs-language.lock.json").write_text(
            json.dumps({"revision": self.revision}), encoding="utf-8"
        )
        (self.root / ".bootstrap").mkdir()
        (self.root / ".bootstrap/revision").write_text("2" * 40, encoding="utf-8")
        (self.root / "tools/bootstrap.sh").write_text("test bootstrap marker\n", encoding="utf-8")
        self.target = self.root / "target"
        self.neighboring_language = self.root.parent / "mncs-language"

    def test_stale_revision_bootstraps_exact_lock_and_unifies_probe_defaults(self) -> None:
        probe = self.target / "release/mncs-compiler-stage0-probe"
        adjacent = self.neighboring_language
        adjacent.mkdir()
        (adjacent / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
        bootstrap = self.root / "tools/bootstrap.sh"
        calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(
            command: list[str],
            *,
            cwd: Path | None = None,
            env: dict[str, str] | None = None,
            **kwargs,
        ):
            calls.append((command, env or {}))
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0)
            self.assertEqual(command, [str(bootstrap)])
            self.assertEqual(cwd, self.root)
            (self.root / ".bootstrap/revision").write_text(self.revision, encoding="utf-8")
            (self.target / ".mncs-language-revision").parent.mkdir(parents=True, exist_ok=True)
            (self.target / ".mncs-language-revision").write_text(
                self.revision, encoding="utf-8"
            )
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"locked probe")
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(
            os.environ, {"MNCS_BOOTSTRAP_TARGET_DIR": str(self.target)}, clear=True
        ):
            os.environ.pop("MNCS_NATIVE_APP_PROBE_BIN", None)
            os.environ.pop("MNCS_PROBE_BIN", None)
            with patch("ensure_locked_stage0_probe.subprocess.run", side_effect=fake_run):
                selected = ensure_locked_stage0_probe(self.root)
            self.assertEqual(selected, probe.resolve())
            self.assertEqual(os.environ.get("MNCS_NATIVE_APP_PROBE_BIN"), str(probe.resolve()))
            self.assertEqual(os.environ.get("MNCS_PROBE_BIN"), str(probe.resolve()))
        bootstrap_call = next(env for command, env in calls if command == [str(bootstrap)])
        self.assertEqual(bootstrap_call.get("MNCS_LANGUAGE_ROOT"), str(adjacent))
        self.assertEqual(bootstrap_call.get("CARGO_BUILD_JOBS"), "4")
        self.assertEqual(
            (self.root / ".bootstrap/revision").read_text(encoding="utf-8").strip(),
            self.revision,
        )

    def test_current_revision_uses_existing_probe_without_rebuilding(self) -> None:
        (self.root / ".bootstrap/revision").write_text(self.revision, encoding="utf-8")
        probe = self.root / ".bootstrap/target/release/mncs-compiler-stage0-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"current probe")
        (self.root / ".bootstrap/target/.mncs-language-revision").write_text(
            self.revision, encoding="utf-8"
        )

        with patch.dict(os.environ, {}, clear=True):
            with patch("ensure_locked_stage0_probe.subprocess.run") as run:
                selected = ensure_locked_stage0_probe(self.root)
            run.assert_not_called()
            self.assertEqual(selected, probe.resolve())
            self.assertEqual(os.environ.get("MNCS_PROBE_BIN"), str(probe.resolve()))

    def test_stale_target_build_revision_reprovisions_existing_probe(self) -> None:
        (self.root / ".bootstrap/revision").write_text(self.revision, encoding="utf-8")
        probe = self.root / ".bootstrap/target/release/mncs-compiler-stage0-probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"stale probe")
        (self.root / ".bootstrap/target/.mncs-language-revision").write_text(
            "2" * 40, encoding="utf-8"
        )
        bootstrap = self.root / "tools/bootstrap.sh"

        def fake_run(command: list[str], *, cwd: Path | None = None, **kwargs):
            self.assertEqual(command, [str(bootstrap)])
            self.assertEqual(cwd, self.root)
            (self.root / ".bootstrap/target/.mncs-language-revision").write_text(
                self.revision, encoding="utf-8"
            )
            probe.write_bytes(b"fresh probe")
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {}, clear=True):
            with patch("ensure_locked_stage0_probe.subprocess.run", side_effect=fake_run) as run:
                selected = ensure_locked_stage0_probe(self.root)
            run.assert_called_once()
            self.assertEqual(selected, probe.resolve())

    def test_explicit_probe_override_is_preserved(self) -> None:
        override = str(self.root / "custom-probe")
        with patch.dict(
            os.environ,
            {"MNCS_NATIVE_APP_PROBE_BIN": override, "MNCS_PROBE_BIN": "stale-probe"},
            clear=True,
        ):
            with patch("ensure_locked_stage0_probe.subprocess.run") as run:
                selected = ensure_locked_stage0_probe(self.root)
            run.assert_not_called()
            self.assertEqual(selected, Path(override))
            self.assertEqual(os.environ.get("MNCS_NATIVE_APP_PROBE_BIN"), override)
            self.assertEqual(os.environ.get("MNCS_PROBE_BIN"), override)


if __name__ == "__main__":
    unittest.main()
