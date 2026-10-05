#!/usr/bin/env python3
"""Direct mncs.vm.artifact/1 emission differential (P-VM-COMPILER-001).

Proves the new proof path end to end without the migration adapter:

    pinned Stage-0 elaboration
      -> direct compiler-owned VM artifact bytes (probe emit_vm_artifact)
      -> live mncs-vm admission + execution (CLI, frozen bytes)
      -> structured result compared against the pinned oracle

Run directly: python3 tools/test_vm_emit.py
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
PROBE = Path(os.environ.get("MNCS_PROBE_BIN", BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))
VM_REPO = ROOT.parent / "mncs-vm"
VM_BIN = Path(os.environ.get("MNCS_VM_BIN", VM_REPO / "target" / "debug" / "mncs-vm"))
os.chdir(ROOT)

MODULE = "mncs.compiler.source.v1"
SEEDS = [{"module": MODULE, "function": "byte_at",
          "type_arguments": [{"kind": "nat", "value": 8}]}]


def ensure_probe():
    if not PROBE.exists():
        raise SystemExit(f"probe binary missing: {PROBE} (run tools/bootstrap.sh first)")


def ensure_vm():
    if VM_BIN.exists():
        return
    if not (VM_REPO / "Cargo.toml").exists():
        raise SystemExit(f"sibling mncs-vm checkout missing: {VM_REPO}")
    print(f"building mncs-vm ({VM_BIN}) ...", flush=True)
    subprocess.run(["cargo", "build", "--offline", "--bin", "mncs-vm"],
                   cwd=VM_REPO, check=True)
    if not VM_BIN.exists():
        raise SystemExit(f"mncs-vm binary still missing: {VM_BIN}")


class Probe:
    def __init__(self):
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = "source"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(SEEDS)
        self.proc = subprocess.Popen(
            [str(PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, env=env, cwd=ROOT)

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"probe terminated: {self.proc.poll()}"
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


def byte_seq(data: bytes):
    return {"sequence": {"values": [{"byte": {"value": b}} for b in data]}}


def u64_arg(value):
    return {"integer": {"value": value, "type": {"bits": 64, "signed": False}}}


def nat_arg(value):
    return {"kind": "nat", "value": value}


def vm_run(artifact_path, function, args, type_args):
    with tempfile.TemporaryDirectory(prefix="mncs-vm-emit-") as directory:
        tmp = Path(directory)
        args_path = tmp / "args.json"
        targs_path = tmp / "targs.json"
        args_path.write_text(json.dumps(args))
        targs_path.write_text(json.dumps(type_args))
        completed = subprocess.run(
            [str(VM_BIN), "run", "--artifact", str(artifact_path),
             "--callable", f"{MODULE}::{function}",
             "--args", str(args_path), "--type-args", str(targs_path)],
            capture_output=True, text=True)
        return completed

def main():
    ensure_probe()
    ensure_vm()
    probe = Probe()
    try:
        first = probe.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = first["artifact"]
        # Contract surface: sealed canonical artifact, no compiler
        # internals (no Program dump), generic rows carried.
        assert artifact["schema_version"] == "mncs.vm.artifact/1", artifact["schema_version"]
        assert artifact["artifact_id"].startswith("sha256:"), artifact["artifact_id"]
        assert "program" not in artifact, "direct bytes must not embed the Program"
        assert set(artifact["code"].keys()) == {"mncs-selected-ssa"}, artifact["code"].keys()
        assert artifact["code"]["mncs-selected-ssa"]["ssa_schema"] == "0.5"
        assert any(e["name"] == "page_count_for" for e in artifact["callables"]), "concrete callable"
        rows = [r for r in artifact.get("generic_entrypoints", [])
                if r["generic_function"] == "byte_at"]
        assert len(rows) == 1 and rows[0]["args_spellings"] == ["8"], rows
        assert rows[0]["canonical_args"], "specialization identity bound"
        assert artifact["source"]["backend_name"] == "mncs-vm-direct"
        refs = artifact["source"]["lowering_refs"]
        assert any(r.startswith("stage0-lock:") for r in refs), refs
        assert artifact["requirements"]["vm_contract"] == "mncs.vm/0.1"
        # Deterministic: re-emission is byte-identical.
        second = probe.send({"emit_vm_artifact": {"module": MODULE}})
        first_bytes = json.dumps(artifact, sort_keys=True).encode()
        second_bytes = json.dumps(second["artifact"], sort_keys=True).encode()
        assert hashlib.sha256(first_bytes).digest() == hashlib.sha256(second_bytes).digest(), \
            "emission must be deterministic"
        # One-shot CLI emits the same bytes as the protocol: byte
        # stability across transports (P-VM-COMPILER-003).
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = "source"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(SEEDS)
        one_shot = subprocess.run(
            [str(PROBE), f"--emit-vm-artifact={MODULE}"],
            capture_output=True, text=True, env=env, cwd=ROOT)
        assert one_shot.returncode == 0, one_shot.stderr[-1000:]
        cli_artifact = json.loads(one_shot.stdout)["artifact"]
        cli_bytes = json.dumps(cli_artifact, sort_keys=True).encode()
        assert hashlib.sha256(cli_bytes).digest() == hashlib.sha256(first_bytes).digest(), \
            "one-shot CLI must match protocol bytes"

        with tempfile.TemporaryDirectory(prefix="mncs-vm-emit-") as directory:
            tmp = Path(directory)
            artifact_path = tmp / "direct.json"
            artifact_path.write_bytes(json.dumps(artifact).encode())

            args = [byte_seq(b"ABCDEFGH"), u64_arg(0)]
            # Pinned oracle over the same compilation.
            oracle = probe.send({
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": "byte_at"},
                "arguments": args,
                "type_arguments": [nat_arg(8)],
                "step_budget": 50000,
            })
            assert oracle["status"] == "returned", oracle
            # Canonical VM over frozen direct bytes.
            completed = vm_run(artifact_path, "byte_at", args, [nat_arg(8)])
            assert completed.returncode == 0, completed.stderr
            document = json.loads(completed.stdout)
            assert document["outcome"] == {"kind": "completed"}, document["outcome"]
            record = document["record"]
            assert record["callable_name"].startswith(f"{MODULE}::byte_at<"), record["callable_name"]
            vm_values = [
                {"integer": {"value": v["Integer"]["value"],
                             "type": {"bits": v["Integer"]["bits"], "signed": v["Integer"]["signed"]}}}
                for v in record["returned"]
            ]
            assert vm_values == oracle["returned"], (vm_values, oracle["returned"])
            assert vm_values == [u64_arg(65)], "concrete expectation"
            # Fail-closed over the same frozen bytes: uncompiled N=7 is
            # a structured invalid_request outcome, not a crash.
            bad = vm_run(artifact_path, "byte_at",
                         [byte_seq(b"ABCDEFG"), u64_arg(0)], [nat_arg(7)])
            assert bad.returncode == 0, bad.stderr
            refusal = json.loads(bad.stdout)["outcome"]
            assert refusal["kind"] == "invalid_request", refusal
            assert "no compiled specialization" in refusal["reason"], refusal
    finally:
        probe.close()
    print("vm_emit: direct emission differential passed "
          "(byte_at<8>=65 agrees across direct-VM, pinned oracle, concrete)")


if __name__ == "__main__":
    sys.exit(main())
