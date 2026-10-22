"""Ensure a fact-reuse harness uses the Language revision in its lockfile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


def ensure_locked_stage0_probe(repository_root: Path) -> Path:
    """Return the pinned Stage-0 probe, rebuilding it when its lock is stale.

    A caller-supplied probe path remains an explicit override. For the default
    path, compare the extracted Stage-0 revision with ``mncs-language.lock``
    before any harness imports the probe adapter. Both the extracted source
    and target-directory revision markers must match; otherwise use the
    existing bootstrap script and prefer a neighboring Language checkout
    that contains the locked commit.
    """
    root = Path(repository_root).resolve()
    explicit_probe = os.environ.get("MNCS_NATIVE_APP_PROBE_BIN")
    if explicit_probe:
        os.environ["MNCS_PROBE_BIN"] = explicit_probe
        return Path(explicit_probe)

    lock_path = root / "mncs-language.lock.json"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        revision = lock["revision"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f"cannot read the pinned Language revision from {lock_path}") from error
    if not isinstance(revision, str) or not revision:
        raise RuntimeError(f"{lock_path} does not contain a Language revision")

    target_setting = os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR")
    target_root = Path(target_setting) if target_setting else Path(".bootstrap/target")
    if not target_root.is_absolute():
        target_root = root / target_root
    probe = target_root / "release/mncs-compiler-stage0-probe"
    revision_marker = root / ".bootstrap/revision"
    target_revision_marker = target_root / ".mncs-language-revision"
    try:
        current_revision = revision_marker.read_text(encoding="utf-8").strip()
    except OSError:
        current_revision = None
    try:
        target_revision = target_revision_marker.read_text(encoding="utf-8").strip()
    except OSError:
        target_revision = None

    if current_revision != revision or target_revision != revision or not probe.is_file():
        bootstrap = root / "tools/bootstrap.sh"
        if not bootstrap.is_file():
            raise RuntimeError(f"locked Stage-0 bootstrap script is missing: {bootstrap}")
        environment = os.environ.copy()
        neighboring_language = root.parent / "mncs-language"
        if not environment.get("MNCS_LANGUAGE_ROOT") and (
            neighboring_language / "Cargo.toml"
        ).is_file():
            has_locked_commit = subprocess.run(
                ["git", "-C", str(neighboring_language), "cat-file", "-e", f"{revision}^{{commit}}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if has_locked_commit.returncode == 0:
                environment["MNCS_LANGUAGE_ROOT"] = str(neighboring_language)
        environment.setdefault("CARGO_BUILD_JOBS", "4")
        subprocess.run([str(bootstrap)], cwd=root, env=environment, check=True)

    try:
        bootstrapped_revision = revision_marker.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(f"Stage-0 bootstrap did not create {revision_marker}") from error
    if bootstrapped_revision != revision:
        raise RuntimeError(
            f"Stage-0 revision {bootstrapped_revision!r} does not match locked Language {revision}"
        )
    try:
        bootstrapped_target_revision = target_revision_marker.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(
            f"Stage-0 bootstrap did not create {target_revision_marker}"
        ) from error
    if bootstrapped_target_revision != revision:
        raise RuntimeError(
            "Stage-0 target revision "
            f"{bootstrapped_target_revision!r} does not match locked Language {revision}"
        )
    if not probe.is_file():
        raise RuntimeError(f"Stage-0 bootstrap did not create the expected probe: {probe}")

    selected_probe = probe.resolve()
    os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(selected_probe)
    os.environ["MNCS_PROBE_BIN"] = str(selected_probe)
    return selected_probe
