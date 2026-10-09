#!/usr/bin/env python3
"""Measure the real stage0-probe child while running a bounded frontier case.

The runner records phase timings in the probe process. This wrapper samples
the process tree rooted at that runner, including time-series resources for the
actual Rust probe child, so caller RSS is not mistaken for compiler execution
RSS and phase events can be correlated with child memory growth.
"""

from __future__ import annotations

import argparse
import json
import hashlib
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
    if len(fields) < 20:
        return None
    try:
        parent_pid = int(fields[1])
        process_group_id = int(fields[2])
        session_id = int(fields[3])
        start_time_ticks = int(fields[19])
    except ValueError:
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
    try:
        executable_path = os.readlink(base / "exe")
    except OSError:
        executable_path = None
    rss_kib = _kib(status.get("VmRSS"))
    hwm_kib = _kib(status.get("VmHWM"))
    swap_kib = _kib(status.get("VmSwap"))
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "process_group_id": process_group_id,
        "session_id": session_id,
        "start_time_ticks": start_time_ticks,
        "executable_path": executable_path,
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
            "parent_pid": sample["parent_pid"],
            "process_group_id": sample["process_group_id"],
            "session_id": sample["session_id"],
            "start_time_ticks": sample["start_time_ticks"],
            "executable_path": sample["executable_path"],
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
    if "mncs-compiler-stage0-probe" in str(current.get("cmdline", "")):
        start_ticks = int(sample["start_time_ticks"])
        elapsed_ms = max(
            0.0,
            (time.monotonic() - start_ticks / TICKS_PER_SECOND) * 1000.0,
        )
        current.setdefault("resource_samples", []).append({
            "elapsed_ms": round(elapsed_ms, 3),
            "rss_kib": sample["rss_kib"],
            "hwm_kib": sample["hwm_kib"],
            "swap_kib": sample["swap_kib"],
            "fd_count": sample["fd_count"],
            "cpu_seconds": sample["cpu_seconds"],
            "cpu_user_seconds": sample["cpu_user_seconds"],
            "cpu_system_seconds": sample["cpu_system_seconds"],
            "io": dict(sample["io"]),
        })


def _probe_resource_summary(records: dict[int, dict[str, object]]) -> dict[str, object]:
    rows = list(records.values())
    probe_rows = [row for row in rows if "mncs-compiler-stage0-probe" in str(row.get("cmdline", ""))]

    def last_io_value(row: dict[str, object], field: str) -> int | None:
        latest = row.get("last_io")
        if isinstance(latest, dict) and field in latest:
            return int(latest[field])
        samples = row.get("resource_samples")
        if isinstance(samples, list):
            for sample in reversed(samples):
                if not isinstance(sample, dict):
                    continue
                io_values = sample.get("io")
                if isinstance(io_values, dict) and field in io_values:
                    return int(io_values[field])
        return None

    io_fields = {
        "rchar": "probe_io_rchar_bytes",
        "wchar": "probe_io_wchar_bytes",
        "read_bytes": "probe_io_read_bytes",
        "write_bytes": "probe_io_write_bytes",
        "cancelled_write_bytes": "probe_io_cancelled_write_bytes",
    }

    def summarize_io_field(field: str) -> int | str:
        values = [last_io_value(row, field) for row in probe_rows]
        if not probe_rows or any(value is None for value in values):
            return "UNKNOWN"
        return sum(int(value) for value in values if value is not None)

    io_summary = {
        output: summarize_io_field(key) for key, output in io_fields.items()
    }
    probe_samples = [
        {
            "pid": row["pid"],
            "start_time_ticks": row["start_time_ticks"],
            **sample,
        }
        for row in probe_rows
        for sample in row.get("resource_samples", [])
        if isinstance(sample, dict)
    ]
    probe_samples.sort(key=lambda sample: (int(sample["start_time_ticks"]), float(sample["elapsed_ms"])))
    return {
        "processes": sorted(rows, key=lambda row: int(row["pid"])),
        "probe_sample_clock": "elapsed_ms is CLOCK_MONOTONIC minus /proc stat start ticks; phase traces use process elapsed from Rust Instant and align within kernel tick resolution plus the configured sampling interval",
        "probe_samples": probe_samples,
        "probe_child_count_observed": len(probe_rows),
        "probe_max_sampled_rss_kib": max((int(row["max_rss_kib"]) for row in probe_rows), default=None),
        "probe_max_observed_hwm_kib": max((int(row["max_hwm_kib"]) for row in probe_rows), default=None),
        "probe_max_fd_count": max((int(row["max_fd_count"]) for row in probe_rows), default=None),
        "probe_last_cpu_seconds": sum(float(row["last_cpu_seconds"]) for row in probe_rows),
        "probe_last_cpu_user_seconds": sum(float(row["last_cpu_user_seconds"]) for row in probe_rows),
        "probe_last_cpu_system_seconds": sum(float(row["last_cpu_system_seconds"]) for row in probe_rows),
        **io_summary,
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


def _probe_rss_peak_kib(records: dict[int, dict[str, object]]) -> int | None:
    """Return the sampled high-water RSS of the actual compiler probe child."""
    values = [
        int(row["max_rss_kib"])
        for row in records.values()
        if "mncs-compiler-stage0-probe" in str(row.get("cmdline", ""))
        and isinstance(row.get("max_rss_kib"), int)
    ]
    return max(values) if values else None


def _signal_verified_probe_children(
    records: dict[int, dict[str, object]], signal_number: int
) -> list[dict[str, object]]:
    """Signal only isolated probe groups whose sampled PID identity still matches."""
    outcomes: list[dict[str, object]] = []
    signaled_groups: set[int] = set()
    for row in records.values():
        cmdline = str(row.get("cmdline", ""))
        if "mncs-compiler-stage0-probe" not in cmdline:
            continue
        pid = row.get("pid")
        pgid = row.get("process_group_id")
        if not isinstance(pid, int) or not isinstance(pgid, int) or pgid in signaled_groups:
            continue
        expected = {
            "start_time_ticks": row.get("start_time_ticks"),
            "executable_path": row.get("executable_path"),
            "process_group_id": pgid,
            "session_id": row.get("session_id"),
        }
        current = _proc_snapshot(pid)
        actual = {
            key: current.get(key) if current is not None else None
            for key in expected
        }
        if (
            current is None
            or any(value is None for value in expected.values())
            or actual != expected
            or pgid != pid
            or row.get("session_id") != pid
            or not isinstance(row.get("executable_path"), str)
        ):
            outcomes.append({
                "pid": pid,
                "process_group_id": pgid,
                "signal": signal.Signals(signal_number).name,
                "outcome": "identity_changed_or_not_isolated",
                "expected_identity": expected,
                "observed_identity": actual,
            })
            continue
        signaled_groups.add(pgid)
        try:
            os.killpg(pgid, signal_number)
            outcome = "sent"
        except ProcessLookupError:
            outcome = "already_exited"
        except PermissionError:
            outcome = "permission_denied"
        outcomes.append({
            "pid": pid,
            "process_group_id": pgid,
            "start_time_ticks": expected["start_time_ticks"],
            "executable_path": expected["executable_path"],
            "signal": signal.Signals(signal_number).name,
            "outcome": outcome,
        })
    return outcomes


def _stop_probe_children(
    proc: subprocess.Popen[bytes],
    records: dict[int, dict[str, object]],
    *,
    grace_seconds: float = 5.0,
) -> dict[str, object]:
    """Interrupt probe children, escalating only after revalidating their identity."""
    result: dict[str, object] = {
        "interrupt_targets": _signal_verified_probe_children(records, signal.SIGINT),
        "kill_targets": [],
        "parent_interrupt_sent": False,
        "parent_kill_sent": False,
    }
    try:
        proc.wait(timeout=grace_seconds)
        return result
    except subprocess.TimeoutExpired:
        pass

    result["kill_targets"] = _signal_verified_probe_children(records, signal.SIGKILL)
    try:
        proc.wait(timeout=grace_seconds)
        return result
    except subprocess.TimeoutExpired:
        pass

    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)
            result["parent_interrupt_sent"] = True
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace_seconds)
            return result
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
            result["parent_kill_sent"] = True
        except ProcessLookupError:
            pass
    proc.wait()
    return result


def _read_phase_trace(path: Path) -> tuple[list[dict[str, object]], list[str], str | None]:
    raw = _read_text(path)
    if raw is None:
        return [], [], None
    events: list[dict[str, object]] = []
    errors: list[str] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"line {line_number}: {error}")
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            errors.append(f"line {line_number}: event is not an object")
    return events, errors, hashlib.sha256(raw.encode()).hexdigest()


def _rss_cap_state(
    records: dict[int, dict[str, object]], limit_mib: int | None
) -> tuple[bool, int | None]:
    """Compare the compiler-child RSS sample with a declared stop threshold."""
    peak_kib = _probe_rss_peak_kib(records)
    return (
        limit_mib is not None and peak_kib is not None and peak_kib >= limit_mib * 1024,
        peak_kib,
    )


def _semantic_result(report: dict[str, object] | None) -> str:
    if report is None:
        return "UNKNOWN_NO_COMPILER_RESULT"
    if report.get("kind") == "compiler-program-cache-preparation":
        return "NOT_RUN_CACHE_PREPARATION"
    if report.get("kind") == "compiler-backend-admission-only":
        return "NOT_RUN_BACKEND_ADMISSION_ONLY"
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
    if report.get("kind") == "compiler-program-cache-preparation":
        trace = report.get("probe_internal_trace")
        events = trace.get("events") if isinstance(trace, dict) else None
        if isinstance(events, list):
            cache_written = any(
                isinstance(event, dict)
                and event.get("event") == "phase"
                and event.get("phase") == "artifact_cache_gzip_write"
                and event.get("cache_file_suffix") == ".program.json.gz"
                and event.get("success") is True
                for event in events
            )
            cache_reused = any(
                isinstance(event, dict)
                and event.get("event") == "phase"
                and event.get("phase") == "frontend_program_cache_read_decode"
                and event.get("usable") is True
                for event in events
            )
            if cache_written or cache_reused:
                return "ADMISSION_ONLY_SUCCESS"
        return "UNKNOWN_CACHE_PREPARATION"
    if report.get("kind") == "compiler-backend-admission-only":
        trace = report.get("probe_internal_trace")
        events = trace.get("events") if isinstance(trace, dict) else None
        if isinstance(events, list):
            backend_admitted = any(
                isinstance(event, dict)
                and event.get("event") == "phase"
                and event.get("phase") == "retained_session_admission"
                and event.get("admitted") is True
                for event in events
            )
            artifact_cached = any(
                isinstance(event, dict)
                and event.get("event") == "phase"
                and event.get("phase") == "artifact_cache_gzip_write"
                and event.get("cache_file_suffix") == ".json.gz"
                and event.get("success") is True
                for event in events
            )
            if backend_admitted and artifact_cached:
                return "ADMISSION_ONLY_SUCCESS"
        return "UNKNOWN_BACKEND_ADMISSION"
    if report.get("kind") == "stage0-project-oracle-frontier":
        oracle = report.get("stage0_oracle")
        if isinstance(oracle, dict) and oracle.get("status") in {"valid", "invalid"}:
            return "STAGE0_ORACLE_COMPLETED"
        return "UNKNOWN_STAGE0_ORACLE_RESULT"
    transport_outcome = report.get("native_transport_outcome")
    if transport_outcome in {"TIMEOUT", "INTERRUPTED", "RESOURCE_EXHAUSTED", "FAILURE", "UNKNOWN"}:
        return str(transport_outcome)
    transport_failure = report.get("transport_failure")
    if isinstance(transport_failure, dict):
        failure_status = transport_failure.get("status")
        if failure_status in {"TIMEOUT", "INTERRUPTED", "RESOURCE_EXHAUSTED", "FAILURE", "UNKNOWN"}:
            return str(failure_status)
    status = report.get("native_request_status")
    if status in {"budget_exhausted", "step_limit_exceeded"}:
        return "RESOURCE_EXHAUSTED"
    if status == "invalid_request":
        return "REJECTED_BEFORE_COMPILER_EXECUTION"
    if status == "returned":
        return "COMPLETED_WITH_RESULT"
    return "UNKNOWN_NO_RETURNED_PROJECT_RESULT"


def _measurement_runner_status(
    *, timed_out: bool, returncode: int, resource_cap_triggered: bool = False
) -> str:
    if timed_out:
        return "TIMEOUT"
    if resource_cap_triggered:
        return "RESOURCE_EXHAUSTED"
    if returncode == 0:
        return "SUCCESS"
    if returncode < 0:
        return "INTERRUPTED"
    return "FAILURE"


def _operation_classification(report: dict[str, object] | None, runner_status: str) -> str:
    """Classify the compiler operation independently from its measurement runner."""
    if runner_status != "SUCCESS":
        return runner_status
    outcome = _operation_outcome(report)
    if outcome in {"TIMEOUT", "INTERRUPTED", "RESOURCE_EXHAUSTED", "FAILURE"}:
        return outcome
    if outcome in {"COMPLETED_WITH_RESULT", "STAGE0_ORACLE_COMPLETED", "ADMISSION_ONLY_SUCCESS"}:
        return "SUCCESS"
    if outcome == "REJECTED_BEFORE_COMPILER_EXECUTION":
        return "FAILURE"
    return "UNKNOWN"


def _nested_probe_stderr_lines(
    report: dict[str, object] | None,
    field: str,
) -> list[str]:
    if not isinstance(report, dict):
        return []
    probe_stderr = report.get("probe_stderr")
    if not isinstance(probe_stderr, dict):
        return []
    values = probe_stderr.get(field)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _prefixed_json_profiles(
    stderr: str,
    prefix: str,
) -> tuple[list[dict[str, object]], list[str]]:
    profiles = []
    errors = []
    for line_number, line in enumerate(stderr.splitlines(), start=1):
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix):])
        except json.JSONDecodeError as error:
            errors.append(f"line {line_number}: {error}")
            continue
        if not isinstance(value, dict):
            errors.append(f"line {line_number}: profile value is not an object")
            continue
        profiles.append(value)
    return profiles, errors


def _body_runtime_profiles(stderr: str) -> tuple[list[dict[str, object]], list[str]]:
    return _prefixed_json_profiles(stderr, "mncs-body-runtime-profile ")


def _body_runtime_progress_events(stderr: str) -> tuple[list[dict[str, object]], list[str]]:
    return _prefixed_json_profiles(stderr, "mncs-body-runtime-progress ")


def _ssa_runtime_profiles(stderr: str) -> tuple[list[dict[str, object]], list[str]]:
    return _prefixed_json_profiles(stderr, "mncs-runtime-profile ")


def _ssa_runtime_progress_events(stderr: str) -> tuple[list[dict[str, object]], list[str]]:
    return _prefixed_json_profiles(stderr, "mncs-ssa-runtime-progress ")


def _named_profile_phases(stderr: str, prefix: str) -> list[dict[str, object]]:
    pattern = re.compile(
        re.escape(prefix) + r" phase=([^\s]+) elapsed_ns=(\d+)"
    )
    return [
        {"phase": match.group(1), "elapsed_ns": int(match.group(2)), "scope": prefix.strip()}
        for line in stderr.splitlines()
        if (match := pattern.search(line)) is not None
    ]


def _body_profile_summaries(
    profiles: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    modules: dict[str, dict[str, int]] = {}
    functions = []
    for profile in profiles:
        rows = profile.get("functions")
        if not isinstance(rows, dict):
            continue
        for identity, raw in rows.items():
            if not isinstance(identity, str) or not isinstance(raw, dict):
                continue
            module_prefix = "mncs:0.2:function:"
            module_part = identity.split("::", 1)[0]
            module = (
                module_part[len(module_prefix):]
                if module_part.startswith(module_prefix)
                else "UNKNOWN"
            )
            fields = {
                name: int(raw.get(name, 0))
                for name in ("calls", "steps", "inclusive_ns", "exclusive_ns")
            }
            aggregate = modules.setdefault(
                module,
                {name: 0 for name in fields},
            )
            for name, value in fields.items():
                aggregate[name] += value
            functions.append({"identity": identity, **fields})
    module_rows = [
        {"module": module, **values}
        for module, values in modules.items()
    ]
    module_rows.sort(key=lambda row: (-row["exclusive_ns"], row["module"]))
    functions.sort(key=lambda row: (-row["exclusive_ns"], row["identity"]))
    return module_rows, functions[:25]


def run(args: argparse.Namespace) -> dict[str, object]:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    label = args.label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selection = "-".join(args.modules)
    base = f"compiler-probe-{label}-{selection}"
    trace_path = ARTIFACTS / f"{base}-phases.jsonl"
    nested_stderr_path = ARTIFACTS / f"{base}-probe-stderr.log"
    measurement_path = ARTIFACTS / f"{base}-measurement.json"
    if trace_path.exists() or nested_stderr_path.exists() or measurement_path.exists():
        raise FileExistsError(f"refusing to overwrite existing measurement {base}")

    env = os.environ.copy()
    env["MNCS_PROBE_BACKEND"] = args.backend
    env["MNCS_PROBE_TELEMETRY"] = "1"
    env["MNCS_PROBE_TRACE_PATH"] = str(trace_path)
    env["MNCS_PROBE_STDERR_PATH"] = str(nested_stderr_path)
    env["MNCS_PROBE_ARTIFACT_LABEL"] = f"{label}-{selection}"
    env["MNCS_TIMINGS"] = "1"
    if args.runtime_profile:
        env["MNCS_RUNTIME_PROFILE"] = "1"
    command = [sys.executable, str(ROOT / "tools" / "probe_compiler_module_frontier.py"), "--modules", *args.modules]
    if args.target_last:
        command.append("--target-last")
    if args.prepare_program_cache_only:
        command.append("--prepare-program-cache-only")
    if args.admission_only:
        command.append("--admission-only")
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
    if args.step_budget is not None:
        command.extend(["--step-budget", str(args.step_budget)])

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
        rss_cap_triggered = False
        rss_cap_triggered_at_seconds: float | None = None
        rss_cap_observed_kib: int | None = None
        termination_actions: dict[str, object] | None = None
        while proc.poll() is None:
            for pid in [proc.pid, *_descendants(proc.pid)]:
                _record_sample(records, pid)
                if cgroup_dir is None and "mncs-compiler-stage0-probe" in str((records.get(pid) or {}).get("cmdline", "")):
                    cgroup_dir = _cgroup_path(pid)
            if args.max_probe_rss_mib is not None:
                reached_cap, rss_peak_kib = _rss_cap_state(records, args.max_probe_rss_mib)
                if reached_cap:
                    rss_cap_triggered = True
                    rss_cap_triggered_at_seconds = time.monotonic() - started
                    rss_cap_observed_kib = rss_peak_kib
                    termination_actions = _stop_probe_children(proc, records)
                    break
            if time.monotonic() >= deadline:
                timed_out = True
                termination_actions = _stop_probe_children(proc, records)
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
    runner_status = _measurement_runner_status(
        timed_out=timed_out, returncode=returncode,
        resource_cap_triggered=rss_cap_triggered,
    )
    classification = _operation_classification(probe_report, runner_status)

    phase_events, phase_trace_errors, phase_trace_sha256 = _read_phase_trace(trace_path)
    probe_start = next(
        (event for event in phase_events if event.get("event") == "probe_start"),
        {},
    )
    execution_mode = (
        "frontend_cache_preparation" if args.prepare_program_cache_only
        else "backend_admission_only" if args.admission_only
        else "reference_interpreter" if args.signature_cache_fixture
        else args.backend
    )
    phases = [event for event in phase_events if event.get("event") == "phase"]
    timing_stderr = "\n".join([
        stderr,
        *_nested_probe_stderr_lines(probe_report, "mncs_timings"),
    ])
    profile_stderr = "\n".join([
        stderr,
        *_nested_probe_stderr_lines(probe_report, "tail"),
    ])
    compiler_timing_events = [
        {"stage": stage, "elapsed_ms": int(milliseconds), "scope": "existing MNCS_TIMINGS event"}
        for stage, milliseconds in re.findall(
            r"mncs-timing stage=([^\s]+) elapsed_ms=(\d+)", timing_stderr
        )
    ]
    runtime_profiles, runtime_profile_parse_errors = _body_runtime_profiles(profile_stderr)
    runtime_progress_events, runtime_progress_parse_errors = (
        _body_runtime_progress_events(profile_stderr)
    )
    runtime_profile_parse_errors.extend(
        f"mncs-body-runtime-progress {error}" for error in runtime_progress_parse_errors
    )
    ssa_runtime_profiles, ssa_profile_parse_errors = _ssa_runtime_profiles(profile_stderr)
    ssa_progress_events, ssa_progress_parse_errors = _ssa_runtime_progress_events(
        profile_stderr
    )
    runtime_profile_parse_errors.extend(
        f"mncs-ssa-runtime-progress {error}" for error in ssa_progress_parse_errors
    )
    runtime_profile_parse_errors.extend(
        f"mncs-runtime-profile {error}" for error in ssa_profile_parse_errors
    )
    ssa_profile_phase_events = _named_profile_phases(profile_stderr, "mncs-ssa-profile")
    artifact_profile_phase_events = _named_profile_phases(
        profile_stderr, "mncs-artifact-profile"
    )
    runtime_profile_modules, runtime_profile_hot_functions = _body_profile_summaries(runtime_profiles)
    cgroup_after = _cgroup_snapshot(cgroup_dir)
    nested_stderr = _read_text(nested_stderr_path)
    nested_stderr_bytes = nested_stderr.encode() if nested_stderr is not None else None
    phase_unknown = [
        "compiler semantic phase labels for parser/checker/proof/CFG/SSA remain UNKNOWN; opt-in function profiles report runtime work without asserting those phase boundaries",
        "SSA validation/fingerprint profile phases describe backend preparation, not the MNCS compiler's semantic parser/checker/proof/CFG/SSA work",
        "per-function CPU time remains UNKNOWN; the runtime profile reports wall time, call count, and executor steps",
        "hardware instruction count is UNKNOWN; no permitted counter is exposed by this execution environment",
        "resource samples align with phase events at kernel tick resolution plus the configured sampling interval; sub-sample peaks remain UNKNOWN",
        "an exit signal alone does not identify OOM; cgroup deltas and process termination evidence are reported separately",
    ]
    result = {
        "schema": "mncs-compiler.probe-resource-measurement/3",
        "label": label,
        "command": command,
        "backend_cli_argument": args.backend,
        "backend_requested": env.get("MNCS_PROBE_BACKEND"),
        "execution_mode": execution_mode,
        "probe_process_backend": probe_start.get("backend"),
        "modules": args.modules,
        "target_last": args.target_last,
        "admission_only": args.admission_only,
        "oracle_only": args.oracle_only,
        "signature_cache_fixture": args.signature_cache_fixture,
        "step_budget": args.step_budget if args.step_budget is not None else 8_000_000,
        "runtime_profile_enabled": bool(env.get("MNCS_RUNTIME_PROFILE")),
        "timeout_seconds": args.timeout_seconds,
        "classification": classification,
        "measurement_runner_status": runner_status,
        "semantic_result": _semantic_result(probe_report),
        "operation_outcome": _operation_outcome(probe_report),
        "returncode": returncode,
        "resource_cap": {
            "kind": "sampled_probe_child_rss",
            "limit_mib": args.max_probe_rss_mib,
            "observed_at_trigger_kib": rss_cap_observed_kib,
            "final_sampled_peak_kib": _probe_rss_peak_kib(records),
            "triggered": rss_cap_triggered,
            "triggered_at_seconds": (round(rss_cap_triggered_at_seconds, 3)
                                      if rss_cap_triggered_at_seconds is not None else None),
            "action": "SIGINT verified isolated compiler-child process group after sampled VmRSS reached limit; escalate only after identity revalidation",
            "sample_interval_seconds": args.sample_seconds,
            "scope_note": "sampling can overshoot between observations; this is an orderly runner stop threshold, not a kernel memory limit",
        },
        "termination_evidence": {
            "measurement_stop_cause": ("sampled_probe_child_rss_cap" if rss_cap_triggered
                                        else "measurement_timeout" if timed_out
                                        else "probe_returned_or_child_terminated"),
            "signals": termination_actions,
            "runner_returncode": returncode,
            "cgroup_memory_events_delta": _cgroup_delta(cgroup_before, cgroup_after)["memory_events"],
        },
        "wall_seconds": round(elapsed, 3),
        "host_mem_available_bytes_before": before_host,
        "host_mem_available_bytes_after": _host_mem_available(),
        "cgroup_before": cgroup_before,
        "cgroup_after": cgroup_after,
        "cgroup_delta": _cgroup_delta(cgroup_before, cgroup_after),
        "resource_observation": _probe_resource_summary(records),
        "phase_trace_path": str(trace_path.relative_to(ROOT)),
        "phase_trace_sha256": phase_trace_sha256,
        "phase_trace_parse_errors": phase_trace_errors,
        "phase_events": len(phase_events),
        "phase_rows": phases,
        "nested_probe_stderr": {
            "path": str(nested_stderr_path.relative_to(ROOT)),
            "available": nested_stderr is not None,
            "bytes": len(nested_stderr_bytes) if nested_stderr_bytes is not None else None,
            "sha256": hashlib.sha256(nested_stderr_bytes).hexdigest()
            if nested_stderr_bytes is not None else None,
            "tail": nested_stderr[-12000:] if nested_stderr is not None else None,
        },
        "compiler_timing_events": compiler_timing_events,
        "body_runtime_profiles": runtime_profiles,
        "body_runtime_progress_events": runtime_progress_events,
        "body_runtime_profile_module_totals": runtime_profile_modules,
        "body_runtime_profile_hot_functions": runtime_profile_hot_functions,
        "ssa_runtime_profiles": ssa_runtime_profiles,
        "ssa_runtime_progress_events": ssa_progress_events,
        "ssa_profile_phase_events": ssa_profile_phase_events,
        "artifact_profile_phase_events": artifact_profile_phase_events,
        "runtime_profile_parse_errors": runtime_profile_parse_errors,
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
    parser.add_argument(
        "--admission-only", action="store_true",
        help="compile and admit the selected backend session, then skip target execution",
    )
    parser.add_argument("--oracle-only", action="store_true", help="run and measure only the Stage-0 project oracle")
    parser.add_argument("--signature-cache-fixture", action="store_true", help="run a reduced two-call imported-signature cache differential through the reference interpreter")
    parser.add_argument("--prepare-program-cache-only", action="store_true", help="elaborate and persist the backend-independent Program cache without admitting a backend or executing the compiler target; requires --backend reference_interpreter")
    parser.add_argument("--step-budget", type=int, help="use a smaller execution step budget for a bounded prefix (maximum 8,000,000)")
    parser.add_argument("--runtime-profile", action="store_true", help="enable opt-in function-level body executor profiling")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--sample-seconds", type=float, default=0.5)
    parser.add_argument(
        "--max-probe-rss-mib", type=int,
        help="orderly stop when the sampled compiler child VmRSS reaches this MiB threshold (1..16384)",
    )
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.step_budget is not None and not 1 <= args.step_budget <= 8_000_000:
        parser.error("--step-budget must be between 1 and 8,000,000")
    if args.max_probe_rss_mib is not None and not 1 <= args.max_probe_rss_mib <= 16_384:
        parser.error("--max-probe-rss-mib must be between 1 and 16384")
    if args.prepare_program_cache_only and args.backend != "reference_interpreter":
        parser.error("--prepare-program-cache-only requires --backend reference_interpreter")
    if args.prepare_program_cache_only and args.signature_cache_fixture:
        parser.error("--prepare-program-cache-only cannot be combined with --signature-cache-fixture")
    if args.admission_only and not args.target_last:
        parser.error("--admission-only requires --target-last")
    if args.admission_only and (
        args.prepare_program_cache_only or args.oracle_only or args.signature_cache_fixture
    ):
        parser.error("--admission-only cannot be combined with cache preparation, oracle-only, or signature-cache fixture modes")
    print(json.dumps(run(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
