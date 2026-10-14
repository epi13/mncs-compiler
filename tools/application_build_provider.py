#!/usr/bin/env python3
"""Environment transport for the compiler-owned native application build contract.

This adapter reads one exact JSON request, invokes the pinned Stage-0 probe,
and publishes its complete versioned result through Environment's invocation
artifact directory. It contains no source-language semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA = "mncs.compiler.application-build-request/1"
RESPONSE_SCHEMA = "mncs.compiler.application-build/1"
INVOCATION_SCHEMA = "mncs.compiler.application-build-invocation/1"
INPUT_ARTIFACT_SCHEMA = "mncs.environment.provider-input-artifacts/1"
INPUT_ARTIFACT_DIR_ENV = "MNCS_ENV_INPUT_ARTIFACT_DIR"
INPUT_ARTIFACTS_ENV = "MNCS_ENV_INPUT_ARTIFACTS_JSON"
OUTPUT_ARTIFACT_DIR_ENV = "MNCS_ENV_SESSION_ARTIFACT_DIR"
MAX_REQUEST_BYTES = 8 * 1024 * 1024


class ProviderError(RuntimeError):
    pass


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def bounded_read(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_REQUEST_BYTES + 1)
    except OSError as error:
        raise ProviderError("application build request is unavailable") from error
    if len(content) > MAX_REQUEST_BYTES:
        raise ProviderError("application build request exceeds the provider input bound")
    return content


def request_file_bytes(raw_path: str) -> bytes:
    requested = Path(raw_path)
    if any(part == ".." for part in requested.parts):
        raise ProviderError("request file path may not traverse parent directories")
    candidate = requested if requested.is_absolute() else ROOT / requested
    try:
        relative = candidate.relative_to(ROOT)
    except ValueError as error:
        raise ProviderError("request file must be inside the selected compiler checkout") from error
    cursor = ROOT
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise ProviderError("request file path is invalid")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProviderError("request file path may not contain symbolic links")
    try:
        resolved = cursor.resolve(strict=True)
    except OSError as error:
        raise ProviderError("application build request is unavailable") from error
    if not resolved.is_relative_to(ROOT) or not resolved.is_file():
        raise ProviderError("request file must be a regular file in the selected compiler checkout")
    return bounded_read(resolved)


def request_artifact_bytes(name: str) -> bytes:
    raw_manifest = os.environ.get(INPUT_ARTIFACTS_ENV, "")
    raw_directory = os.environ.get(INPUT_ARTIFACT_DIR_ENV, "")
    if not raw_manifest or not raw_directory:
        raise ProviderError("named Store request artifact was not supplied by Environment")
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as error:
        raise ProviderError("Environment input-artifact manifest is invalid") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != INPUT_ARTIFACT_SCHEMA
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise ProviderError("Environment input-artifact manifest schema is unsupported")
    matches = [
        item for item in manifest["artifacts"]
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ProviderError("named Store request artifact must resolve exactly once")
    item = matches[0]
    relative = Path(str(item.get("local_path", "")))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ProviderError("Environment input-artifact path is invalid")
    root = Path(raw_directory)
    if root.is_symlink():
        raise ProviderError("Environment input-artifact directory is invalid")
    try:
        root = root.resolve(strict=True)
        candidate = root / relative
        if candidate.is_symlink():
            raise ProviderError("Environment input-artifact file is invalid")
        path = candidate.resolve(strict=True)
    except OSError as error:
        raise ProviderError("Environment input-artifact file is unavailable") from error
    if not path.is_relative_to(root) or not path.is_file():
        raise ProviderError("Environment input-artifact file is outside its private directory")
    content = bounded_read(path)
    if (
        item.get("byte_count") != len(content)
        or item.get("sha256") != hashlib.sha256(content).hexdigest()
        or not isinstance(item.get("store_reference"), str)
        or not item.get("store_reference")
    ):
        raise ProviderError("Environment input-artifact bytes do not match Store provenance")
    return content


def selected_probe() -> Path:
    configured = os.environ.get("MNCS_COMPILER_PROBE")
    candidate = Path(configured) if configured else (
        ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe"
    )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ProviderError("selected Stage-0 compiler probe is unavailable") from error
    if not resolved.is_relative_to(ROOT) or not resolved.is_file():
        raise ProviderError("selected Stage-0 compiler probe is outside the compiler checkout")
    return resolved


def selected_cache(raw: str | None) -> Path:
    candidate = Path(raw) if raw else (
        ROOT / ".build/native-app-readiness/application-build-cache"
    )
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    if any(part == ".." for part in candidate.parts):
        raise ProviderError("application cache path may not traverse parent directories")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(ROOT)
    except (OSError, ValueError) as error:
        raise ProviderError("application cache must remain inside the compiler checkout") from error
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def run_build(request_bytes: bytes, *, cache: Path, timeout: float) -> dict:
    try:
        request = json.loads(request_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("application build request is not valid UTF-8 JSON") from error
    if not isinstance(request, dict):
        raise ProviderError("application build request must be a JSON object")
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise ProviderError("application build request schema is unsupported")
    probe = selected_probe()
    environment = os.environ.copy()
    environment["MNCS_PROBE_MODULES"] = ""
    environment["MNCS_PROBE_EXECUTION_MODULES"] = ""
    environment["MNCS_PROBE_GENERIC_SEEDS"] = "[]"
    environment.pop("MNCS_PROBE_BACKEND", None)
    environment["MNCS_PROBE_CACHE_DIR"] = str(cache)
    environment["MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT"] = str(ROOT)
    wire = (
        json_bytes({"execution_status": True})
        + b"\n"
        + json_bytes({"compile_application": request})
        + b"\n"
    )
    try:
        completed = subprocess.run(
            [str(probe)],
            cwd=ROOT,
            env=environment,
            input=wire,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderError("Stage-0 application build invocation failed or timed out") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-1200:]
        raise ProviderError(
            f"Stage-0 application build process exited {completed.returncode}: {detail}"
        )
    lines = completed.stdout.splitlines()
    if len(lines) < 2:
        raise ProviderError("Stage-0 application build returned no structured response")
    try:
        status = json.loads(lines[0])
        response = json.loads(lines[-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError("Stage-0 application build returned invalid JSON") from error
    if not isinstance(status, dict) or not isinstance(response, dict):
        raise ProviderError("Stage-0 application build returned an invalid response shape")
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise ProviderError("Stage-0 application build returned an unsupported response schema")
    if response.get("status") not in {
        "verified", "unsupported", "unavailable", "unknown", "rejected"
    }:
        raise ProviderError("Stage-0 application build returned an unknown status")
    return response


def publish_response(response: dict) -> str | None:
    raw_directory = os.environ.get(OUTPUT_ARTIFACT_DIR_ENV, "")
    if not raw_directory:
        return None
    directory = Path(raw_directory)
    if directory.is_symlink():
        raise ProviderError("Environment output-artifact directory is invalid")
    try:
        directory = directory.resolve(strict=True)
    except OSError as error:
        raise ProviderError("Environment output-artifact directory is unavailable") from error
    if not directory.is_dir():
        raise ProviderError("Environment output-artifact target is not a directory")
    output = directory / "native-application-build.json"
    data = json_bytes(response)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(output, 0o400, follow_symlinks=False)
    except OSError as error:
        raise ProviderError("Environment application-build result could not be published") from error
    return output.name


def summary(response: dict, output_name: str | None) -> dict:
    return {
        "schema_version": INVOCATION_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
        "status": "stored" if output_name else "available",
        "build_status": response.get("status"),
        "entry": response.get("entry"),
        "source_snapshot_identity": (
            response.get("source_snapshot", {}).get("identity")
            if isinstance(response.get("source_snapshot"), dict) else None
        ),
        "dependency_graph_identity": (
            response.get("dependency_graph", {}).get("identity")
            if isinstance(response.get("dependency_graph"), dict) else None
        ),
        "artifact_identity": (
            response.get("artifact", {}).get("identity")
            if isinstance(response.get("artifact"), dict) else None
        ),
        "result_file": output_name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    build = subparsers.add_parser("build")
    source = build.add_mutually_exclusive_group(required=True)
    source.add_argument("--request-file")
    source.add_argument("--request-artifact")
    build.add_argument("--cache-dir")
    build.add_argument("--timeout-seconds", type=float, default=120.0)
    args = parser.parse_args(argv)
    try:
        raw = (
            request_file_bytes(args.request_file)
            if args.request_file is not None
            else request_artifact_bytes(args.request_artifact)
        )
        cache = selected_cache(args.cache_dir)
        response = run_build(raw, cache=cache, timeout=args.timeout_seconds)
        output_name = publish_response(response)
        value = summary(response, output_name) if output_name else response
        sys.stdout.buffer.write(json_bytes(value) + b"\n")
        return 0
    except (ProviderError, OSError, ValueError) as error:
        sys.stderr.write(json.dumps({
            "schema_version": INVOCATION_SCHEMA,
            "status": "error",
            "reason": str(error),
        }, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
