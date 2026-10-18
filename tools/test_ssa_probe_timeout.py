#!/usr/bin/env python3
"""Check probe response and shutdown deadlines clean up their process group."""
from __future__ import annotations

import tempfile
from pathlib import Path

import test_ssa_sequence_length as support


def fake_probe(directory: Path, *, read_stdin: bool, partial_output: bool = False) -> Path:
    executable = directory / ("probe-close-timeout" if read_stdin else "probe-response-timeout")
    body = "import sys, time\n"
    if read_stdin:
        body += "sys.stdin.read()\n"
    if partial_output:
        body += "sys.stdout.write('{\\\"partial\\\":')\nsys.stdout.flush()\n"
    body += "time.sleep(30)\n"
    executable.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def check_response_timeout() -> None:
    original_probe = support.PROBE
    try:
        with tempfile.TemporaryDirectory(prefix="mncs-probe-timeout-") as temporary:
            support.PROBE = fake_probe(
                Path(temporary), read_stdin=False, partial_output=True
            )
            probe = support.Probe(
                "mncs.compiler.ssa.v1", [], timeout_seconds=0.1
            )
            try:
                probe.send({"target": "never-responds"})
            except TimeoutError as error:
                assert "response" in str(error), error
            else:
                raise AssertionError("a silent probe exceeded no response deadline")
            assert probe.proc.poll() is not None, "timed-out probe process is still live"
    finally:
        support.PROBE = original_probe


def check_successful_response() -> None:
    original_probe = support.PROBE
    probe = None
    try:
        with tempfile.TemporaryDirectory(prefix="mncs-probe-response-") as temporary:
            executable = Path(temporary) / "probe-echo"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    print(json.dumps(json.loads(line)), flush=True)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            support.PROBE = executable
            probe = support.Probe(
                "mncs.compiler.ssa.v1", [], timeout_seconds=1
            )
            request = {"target": "echo"}
            assert probe.send(request) == request
            assert probe.close() == ""
    finally:
        if probe is not None and probe.proc.poll() is None:
            probe._terminate()
            if not probe.stderr_file.closed:
                probe.stderr_file.close()
        support.PROBE = original_probe


def check_shutdown_timeout() -> None:
    original_probe = support.PROBE
    try:
        with tempfile.TemporaryDirectory(prefix="mncs-probe-shutdown-") as temporary:
            support.PROBE = fake_probe(Path(temporary), read_stdin=True)
            probe = support.Probe(
                "mncs.compiler.ssa.v1", [], timeout_seconds=0.1
            )
            try:
                probe.close()
            except TimeoutError as error:
                assert "shutdown" in str(error), error
            else:
                raise AssertionError("a probe that ignored EOF exceeded no shutdown deadline")
            assert probe.proc.poll() is not None, "timed-out probe process is still live"
    finally:
        support.PROBE = original_probe


def main() -> None:
    check_successful_response()
    check_response_timeout()
    check_shutdown_timeout()
    print("Stage-0 probe response and shutdown timeouts terminate their process groups")


if __name__ == "__main__":
    main()
