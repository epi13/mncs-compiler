"""Bootstrap retirement uses successful builds and preserves source provenance."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BootstrapStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mncs-bootstrap-storage-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "compiler"
        (self.repository / "tools").mkdir(parents=True)
        shutil.copy2(ROOT / "tools/bootstrap.sh", self.repository / "tools/bootstrap.sh")
        self.source = self.base / "language"
        self.source.mkdir()
        (self.source / "Cargo.toml").write_text("[workspace]\nmembers=[]\n")
        (self.source / "provenance.txt").write_text("pinned source bytes\n")
        self.git("init", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.name=Storage Test", "-c", "user.email=storage@example.invalid", "commit", "-m", "pin")
        self.revision = self.git("rev-parse", "HEAD")
        (self.repository / "mncs-language.lock.json").write_text(json.dumps({"revision": self.revision}))
        self.previous = "a" * 40
        bootstrap = self.repository / ".bootstrap"
        (bootstrap / "target/release").mkdir(parents=True)
        (bootstrap / "revision").write_text(self.previous + "\n")
        (bootstrap / "Cargo.toml").write_text("[workspace]\nmembers=[]\n")
        (bootstrap / "source-to-preserve").write_text("historical source\n")
        (bootstrap / "target/release/old-compiler").write_bytes(b"disposable build\n")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        cargo = self.bin / "cargo"
        cargo.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args=sys.argv[1:]
if args[0] == 'build':
    if os.environ.get('FAIL_BUILD') == '1': sys.exit(9)
    target=pathlib.Path(os.environ['CARGO_TARGET_DIR'])
    (target/'release').mkdir(parents=True, exist_ok=True)
    (target/'release/new-compiler').write_bytes(b'new build')
elif args[0] == 'clean':
    target=pathlib.Path(args[args.index('--target-dir')+1])
    if target.exists(): shutil.rmtree(target)
else: sys.exit(99)
''')
        cargo.chmod(0o755)

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(["git", "-C", str(self.source), *arguments], text=True, stderr=subprocess.DEVNULL).strip()

    def run_bootstrap(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "MNCS_LANGUAGE_ROOT": str(self.source)}
        environment.pop("MNCS_BOOTSTRAP_TARGET_DIR", None)
        environment.pop("MNCS_BOOTSTRAP_KEEP_ARCHIVED_TARGETS", None)
        environment.update(overrides)
        return subprocess.run(["bash", "tools/bootstrap.sh", *arguments], cwd=self.repository, env=environment, text=True, capture_output=True, timeout=20)

    def test_successful_repin_clears_archived_build_outputs_and_preserves_source(self) -> None:
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.repository / ".build/stage0" / self.previous
        self.assertTrue((archive / "source-to-preserve").is_file())
        self.assertFalse((archive / "target").exists())
        self.assertEqual((self.repository / ".bootstrap/revision").read_text().strip(), self.revision)
        self.assertTrue((self.repository / ".bootstrap/target/release/new-compiler").is_file())

    def test_failed_build_preserves_previous_outputs_until_a_successful_retry(self) -> None:
        result = self.run_bootstrap(FAIL_BUILD="1")
        self.assertEqual(result.returncode, 9, result.stderr)
        archive = self.repository / ".build/stage0" / self.previous
        self.assertTrue((archive / "target/release/old-compiler").exists())
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((archive / "source-to-preserve").exists())
        self.assertFalse((archive / "target").exists())

    def test_explicit_retention_keeps_historical_builds(self) -> None:
        result = self.run_bootstrap(MNCS_BOOTSTRAP_KEEP_ARCHIVED_TARGETS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repository / ".build/stage0" / self.previous / "target/release/old-compiler").exists())

    def test_unknown_directories_and_symlink_targets_are_preserved(self) -> None:
        archive = self.repository / ".build/stage0" / ("b" * 40)
        archive.mkdir(parents=True)
        (archive / "revision").write_text("b" * 40)
        (archive / "Cargo.toml").write_text("[workspace]\nmembers=[]\n")
        external = self.base / "explicit-target"
        external.mkdir()
        (external / "keep").write_bytes(b"selected outside storage")
        (archive / "target").symlink_to(external, target_is_directory=True)
        unknown = self.repository / ".build/stage0/manual-experiment/target"
        unknown.mkdir(parents=True)
        (unknown / "keep").write_bytes(b"unknown output")
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((external / "keep").exists())
        self.assertTrue((unknown / "keep").exists())

    def test_maintenance_requires_a_verified_current_build_and_runs_without_rebuilding(self) -> None:
        refused = self.run_bootstrap("--retire-archived-targets")
        self.assertEqual(refused.returncode, 2)
        self.assertTrue((self.repository / ".bootstrap/target/release/old-compiler").exists())
        success = self.run_bootstrap(MNCS_BOOTSTRAP_KEEP_ARCHIVED_TARGETS="1")
        self.assertEqual(success.returncode, 0, success.stderr)
        maintained = self.run_bootstrap("--retire-archived-targets", FAIL_BUILD="1")
        self.assertEqual(maintained.returncode, 0, maintained.stderr)
        archive = self.repository / ".build/stage0" / self.previous
        self.assertFalse((archive / "target").exists())
        self.assertTrue((archive / "source-to-preserve").exists())


if __name__ == "__main__":
    unittest.main()
