#!/usr/bin/env python3
"""Measure the real stage0-probe child while running a bounded frontier case.

The runner records phase timings in the probe process. This wrapper samples
the process tree rooted at that runner, including the actual Rust probe child,
so caller RSS is not mistaken for compiler execution RSS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".build" / "campaign-artifacts" / "compiler-facts"
TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, ProcessLookupError):
        return None


def _proc_snapshot(pid: int) -> dict[str, object] | None:
    base = Path("/proc") / str(pid)
    status_text = _read_text(base / "status")
    stat_text = _read_text(base / "stat")
    if status_text is None or stat_text is None:
        return None

    status: dict[str, str] = {}
    for line in status_text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            status[key] = value.strip()

    open_paren = stat_text.find("(")
    close_paren = stat_text.rfind(")")
    if open_paren < 0 or close_paren < 0:
        return None
    comm = stat_text[open_paren + 1 : close_paren]
    fields = stat_text[close_paren + 2 :].split()  # starts at proc stat field 3
    if len(fields) < 13:
        return None
    cpu_user_seconds = int(fields[11]) / TICKS_PER_SECOND
    cpu_system_seconds = int(fields[12]) / TICKS_PER_SECOND
    cpu_seconds = cpu_user_seconds + cpu_system_seconds

    io: dict[str, int] = {}
    io_text = _read_text(base / "io")
    if io_text:
        for line in io_text.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                try:
                    io[key] = int(value.strip())
                except ValueError:
                    pass

    try:
        fd_count = len(list((base / "fd").iterdir()))
    except OSError:
        fd_count = None

    cmdline_raw = _read_text(base / "cmdline") or ""
    cmdline = cmdline_raw.replace("\x00", " ").strip()
    rss_kib = _kib(status.get("VmRSS"))
    hwm_kib = _kib(status.get("VmHWM"))
    swap_kib = _kib(status.get("VmSwap"))
    return {
        "pid": pid,
        "comm": comm,
        "cmdline": cmdline,
        "rss_kib": rss_kib,
        "hwm_kib": hwm_kib,
        "swap_kib": swap_kib,
        "fd_count": fd_count,
        "cpu_seconds": cpu_seconds,
        "cpu_user_seconds": cpu_user_seconds,
        "cpu_system_seconds": cpu_system_seconds,
        "io": io,
    }


def _kib(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split()[0])
    except (ValueError, IndexError):
        return None


def _descendants(root_pid: int) -> list[int]:
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        text = _read_text(Path("/proc") / str(parent) / "task" / str(parent) / "children")
        if not text:
            continue
        for item in text.split():
            try:
                child = int(item)
            except ValueError:
                continue
            if child not in found:
                found.add(child)
                pending.append(child)
    return sorted(found)


def _cgroup_path(pid: int) -> Path | None:
    text = _read_text(Path("/proc") / str(pid) / "cgroup")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            candidate = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
            return candidate if candidate.is_dir() else None
    return None


def _read_pairs(path: Path) -> dict[str, str] | None:
    text = _read_text(path)
    if text is None:
        return None
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0].rstrip(":")] = parts[1]
    return result


def _host_mem_available() -> int | None:
    text = _read_text(Path("/proc/meminfo"))
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            value = _kib(line.partition(":")[2])
            return None if value is None else value * 1024
    return None


def _pressure(path: Path) -> dict[str, str] | None:
    text = _read_text(path)
    if text is None:
        return None
    return {row.split()[0]: " ".join(row.split()[1:]) for row in text.splitlines() if row.split()}


def _cgroup_snapshot(directory: Path | None) -> dict[str, object]:
    if directory is None:
        return {"available": False}
    return {
        "path": str(directory),
        "scope_note": "kernel-reported cgroup may include sibling processes; current/peak and event deltas are cgroup-level, while per-process RSS/CPU/I/O below is sampled by PID",
        "memory.current": _read_text(directory / "memory.current"),
        "memory.peak": _read_text(directory / "memory.peak"),
        "memory.max": _read_text(directory / "memory.max"),
        "memory.events": _read_pairs(directory / "memory.events"),
        "cpu.stat": _read_pairs(directory / "cpu.stat"),
        "memory.pressure": _pressure(directory / "memory.pressure"),
    }


def _record_sample(records: dict[int, dict[str, object]], pid: int) -> None:
    sample = _proc_snapshot(pid)
    if sample is None:
        return
    current = records.setdefault(
        pid,
        {
            "pid": pid,
            "comm": sample["comm"],
            "cmdline": sample["cmdline"],
            "samples": 0,
            "max_rss_kib": 0,
            "max_hwm_kib": 0,
            "max_fd_count": 0,
            "last_cpu_seconds": 0.0,
            "last_cpu_user_seconds": 0.0,
            "last_cpu_system_seconds": 0.0,
            "last_io": {},
        },
    )
    current["samples"] = int(current["samples"]) + 1
    for source, target in (("rss_kib", "max_rss_kib"), ("hwm_kib", "max_hwm_kib"), ("fd_count", "max_fd_count")):
        value = sample.get(source)
        if isinstance(value, int):
            current[target] = max(int(current[target]), value)
    current["last_cpu_seconds"] = sample["cpu_seconds"]
    current["last_cpu_user_seconds"] = sample["cpu_user_seconds"]
    current["last_cpu_system_seconds"] = sample["cpu_system_seconds"]
    current["last_io"] = sample["io"]
    current["last_rss_kib"] = sample["rss_kib"]
    current["last_hwm_kib"] = sample["hwm_kib"]
    current["last_swap_kib"] = sample["swap_kib"]
    current["last_fd_count"] = sample["fd_count"]


def _probe_resource_summary(records: dict[int, dict[str, object]]) -> dict[str, object]:
    rows = list(records.values())
    probe_rows = [row for row in rows if "mncs-compiler-stage0-probe" in str(row.get("cmdline", ""))]
    return {
        "processes": sorted(rows, key=lambda row: int(row["pid"])),
        "probe_child_count_observed": len(probe_rows),
        "probe_max_sampled_rss_kib": max((int(row["max_rss_kib"]) for row in probe_rows), default=None),
        "probe_max_observed_hwm_kib": max((int(row["max_hwm_kib"]) for row in probe_rows), default=None),
        "probe_max_fd_count": max((int(row["max_fd_count"]) for row in probe_rows), default=None),
        "probe_last_cpu_seconds": sum(float(row["last_cpu_seconds"]) for row in probe_rows),
        "probe_last_cpu_user_seconds": sum(float(row["last_cpu_user_seconds"]) for row in probe_rows),
        "probe_last_cpu_system_seconds": sum(float(row["last_cpu_system_seconds"]) for row in probe_rows),
        "probe_io_rchar_bytes": sum(int(row.get("last_io", {}).get("rchar", 0)) for row in probe_rows),
        "probe_io_read_bytes": sum(int(row.get("last_io", {}).get("read_bytes", 0)) for row in probe_rows),
    }


def _cgroup_delta(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    def numeric_delta(key: str) -> dict[str, int | str]:
        before_values = before.get(key) or {}
        after_values = after.get(key) or {}
        delta: dict[str, int | str] = {}
        if isinstance(before_values, dict) and isinstance(after_values, dict):
            for name in set(before_values) & set(after_values):
                try:
                    delta[name] = int(after_values[name]) - int(before_values[name])
                except (TypeError, ValueError):
                    delta[name] = "UNKNOWN"
        return delta

    pressure_delta: dict[str, int | str] = {}
    before_pressure = before.get("memory.pressure") or {}
    after_pressure = after.get("memory.pressure") or {}
    if isinstance(before_pressure, dict) and isinstance(after_pressure, dict):
        for name in set(before_pressure) & set(after_pressure):
            before_total = re.search(r"(?:^|\s)total=(\d+)", str(before_pressure[name]))
            after_total = re.search(r"(?:^|\s)total=(\d+)", str(after_pressure[name]))
            pressure_delta[name] = (
                int(after_total.group(1)) - int(before_total.group(1))
                if before_total and after_total else "UNKNOWN"
            )
    return {
        "memory_events": numeric_delta("memory.events"),
        "cpu_stat": numeric_delta("cpu.stat"),
        "memory_pressure_total_usec": pressure_delta,
    }


def _semantic_result(report: dict[str, object] | None) -> str:
    if report is None:
        return "UNKNOWN_NO_COMPILER_RESULT"
    if report.get("kind") == "stage0-project-oracle-frontier":
        return "UNKNOWN_NO_NATIVE_COMPILER_RESULT"
    status = report.get("native_request_status")
    if status == "invalid_request":
        return "REJECTED_BEFORE_COMPILER_EXECUTION"
    if status != "returned":
        return "UNKNOWN_NO_RETURNED_PROJECT_RESULT"
    project_valid = report.get("native_project_valid")
    ssa_valid = report.get("native_project_ssa_valid")
    if project_valid is False:
        return "SEMANTIC_REJECTION"
    if project_valid is True and ssa_valid is False:
        return "PROJECT_VALID_SSA_REJECTION"
    if project_valid is True and ssa_valid is True:
        return "PROJECT_AND_SSA_VALID"
    return "RETURNED_UNCLASSIFIED"


def _operation_outcome(report: dict[str, object] | None) -> str:
    if report is None:
        return "UNKNOWN_NO_COMPILER_RESULT"
    if report.get("kind") == "stage0-project-oracle-frontier":
        return "STAGE0_ORACLE_COMPLETED"
    status = report.get("native_request_status")
    if status in {"budget_exhausted", "step_limit_exceeded"}:
        return "RESOURCE_EXHAUSTED_STEP_BUDGET"
    if status == "invalid_request":
        return "REJECTED_BEFORE_COMPILER_EXECUTION"
    if status == "returned":
        return "COMPLETED_WITH_RESULT"
    return "UNKNOWN_NO_RETURNED_PROJECT_RESULT"


def run(args: argparse.Namespace) -> dict[str, object]:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    label = args.label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selection = "-".join(args.modules)
    base = f"compiler-probe-{label}-{selection}"
    trace_path = ARTIFACTS / f"{base}-phases.jsonl"
    measurement_path = ARTIFACTS / f"{base}-measurement.json"
    if trace_path.exists() or measurement_path.exists():
        raise FileExistsError(f"refusing to overwrite existing measurement {base}")

    env = os.environ.copy()
    env["MNCS_PROBE_BACKEND"] = args.backend
    env["MNCS_PROBE_TELEMETRY"] = "1"
    env["MNCS_PROBE_TRACE_PATH"] = str(trace_path)
    env["MNCS_PROBE_ARTIFACT_LABEL"] = f"{label}-{selection}"
    env["MNCS_TIMINGS"] = "1"
    command = [sys.executable, str(ROOT / "tools" / "probe_compiler_module_frontier.py"), "--modules", *args.modules]
    if args.target_last:
        command.append("--target-last")
    if args.oracle_only:
        command.append("--oracle-only")
    if args.signature_cache_fixture:
        command = [sys.executable, str(ROOT / "tools" / "probe_compiler_module_frontier.py"), "--signature-cache-fixture"]
        env["MNCS_PROBE_BACKEND"] = "reference_interpreter"
        env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.project.v1"
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": "mncs.compiler.project.v1",
            "function": "compile_project_target",
            "type_arguments": [{"kind": "nat", "value": 1024}, {"kind": "nat", "value": 1024}],
        }])
        env["MNCS_PROBE_CACHE_DIR"] = str(ROOT / ".build" / "probe-cache")

    before_host = _host_mem_available()
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        cgroup_dir = _cgroup_path(proc.pid)
        cgroup_before = _cgroup_snapshot(cgroup_dir)
        records: dict[int, dict[str, object]] = {}
        deadline = started + args.timeout_seconds
        timed_out = False
        while proc.poll() is None:
            for pid in [proc.pid, *_descendants(proc.pid)]:
                _record_sample(records, pid)
                if cgroup_dir is None and "mncs-compiler-stage0-probe" in str((records.get(pid) or {}).get("cmdline", "")):
                    cgroup_dir = _cgroup_path(pid)
            if time.monotonic() >= deadline:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                break
            time.sleep(args.sample_seconds)
        returncode = proc.wait()
        elapsed = time.monotonic() - started
        for pid in [proc.pid, *_descendants(proc.pid)]:
            _record_sample(records, pid)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", errors="replace")
        stderr = stderr_file.read().decode("utf-8", errors="replace")

    try:
        probe_report = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        probe_report = None
    if timed_out:
        classification = "TIMEOUT"
    elif returncode == 0:
        classification = "SUCCESS"
    elif returncode < 0:
        classification = f"INTERRUPTED_SIGNAL_{-returncode}_CAUSE_UNKNOWN"
    else:
        classification = "FAILURE"

    try:
        phase_events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        phase_events = []
    probe_start = next(
        (event for event in phase_events if event.get("event") == "probe_start"),
        {},
    )
    execution_mode = (
        "reference_interpreter" if args.signature_cache_fixture else args.backend
    )
    phases = [event for event in phase_events if event.get("event") == "phase"]
    compiler_timing_events = [
        {"stage": stage, "elapsed_ms": int(milliseconds), "scope": "existing MNCS_TIMINGS event"}
        for stage, milliseconds in re.findall(
            r"mncs-timing stage=([^\s]+) elapsed_ms=(\d+)", stderr
        )
    ]
    cgroup_after = _cgroup_snapshot(cgroup_dir)
    phase_unknown = [
        "parser/checker/proof/CFG/SSA subphase wall and CPU time within compile_project are UNKNOWN; the current MNCS entry performs them in one call",
        "hardware instruction count is UNKNOWN; no permitted counter is exposed by this execution environment",
        "an exit signal alone does not identify OOM; cgroup deltas and process termination evidence are reported separately",
    ]
    result = {
        "schema": "mncs-compiler.probe-resource-measurement/1",
        "label": label,
        "command": command,
        "backend_cli_argument": args.backend,
        "backend_requested": env.get("MNCS_PROBE_BACKEND"),
        "execution_mode": execution_mode,
        "probe_process_backend": probe_start.get("backend"),
        "modules": args.modules,
        "target_last": args.target_last,
        "oracle_only": args.oracle_only,
        "signature_cache_fixture": args.signature_cache_fixture,
        "timeout_seconds": args.timeout_seconds,
        "classification": classification,
        "semantic_result": _semantic_result(probe_report),
        "operation_outcome": _operation_outcome(probe_report),
        "returncode": returncode,
        "wall_seconds": round(elapsed, 3),
        "host_mem_available_bytes_before": before_host,
        "host_mem_available_bytes_after": _host_mem_available(),
        "cgroup_before": cgroup_before,
        "cgroup_after": cgroup_after,
        "cgroup_delta": _cgroup_delta(cgroup_before, cgroup_after),
        "resource_observation": _probe_resource_summary(records),
        "phase_trace_path": str(trace_path.relative_to(ROOT)),
        "phase_events": len(phase_events),
        "phase_rows": phases,
        "compiler_timing_events": compiler_timing_events,
        "probe_report": probe_report,
        "stderr_tail": stderr[-12000:],
        "phase_unknown": phase_unknown,
    }
    measurement_path.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["research-bytecode", "cranelift", "canonical-vm", "reference_interpreter"], default="research-bytecode")
    parser.add_argument("--modules", nargs="+", default=["source", "lexer", "parser", "segment", "decl", "flow"])
    parser.add_argument("--target-last", action="store_true")
    parser.add_argument("--oracle-only", action="store_true", help="run and measure only the Stage-0 project oracle")
    parser.add_argument("--signature-cache-fixture", action="store_true", help="run a reduced two-call imported-signature cache differential through the reference interpreter")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--sample-seconds", type=float, default=0.5)
    parser.add_argument("--label")
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
