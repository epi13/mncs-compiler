#!/usr/bin/env python3
"""End-to-end verification of the bounded native MNCS VM emitter
(`mncs.compiler.vm_emit.v1` + `tools/native_vm_artifact.py` -> fresh `mncs-vm`).

Proves:
1. Real stdlib module `std/utf8_primitives.mncs` compiled through native MNCS
   declaration proof -> Flow -> verified Value SSA -> `mncs.compiler.vm_emit.v1`,
   packaged into canonical `mncs.vm.artifact/1`, and independently admitted by
   a fresh `mncs-vm` process.
2. Bit-for-bit deterministic emission across two independent probe processes.
3. Differential execution of 672 inputs across all 3 callables in a fresh
   `mncs-vm serve` process against locked Stage-0 (`mncs-compiler-stage0-probe`),
   recording VM `memory_cells` and `steps` separately from resident memory.
4. Broader scalar + direct-call + bitwise + shift + select + arithmetic +
   checked-overflow witness differentially verified against locked Stage-0.
5. Fail-closed rejection of unverified SSA, unsupported non-scalar types,
   unsupported bounded loops, and tampered artifact bytes at VM admission.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import backend_policy
import native_vm_artifact
import test_sem as sem
import test_vm_emit as vm_emit_test


def _find_repo_root():
    """Return the main checkout root when running inside a linked worktree."""
    try:
        common = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        common_path = (ROOT / common).resolve()
        if common_path.name == ".git":
            return common_path.parent
    except (OSError, subprocess.CalledProcessError):
        pass
    return ROOT


MAIN_ROOT = _find_repo_root()
PROBE_BIN = Path(
    os.environ.get(
        "MNCS_PROBE_BIN",
        MAIN_ROOT / ".bootstrap" / "target" / "release" / "mncs-compiler-stage0-probe",
    )
)
STDLIB_ROOT = Path(
    os.environ.get("MNCS_STDLIB_ROOT", MAIN_ROOT.parent / "mncs-stdlib")
)
VM_REPO = vm_emit_test.resolve_vm_repo()
VM_RELEASE = VM_REPO / "target" / "release" / "mncs-vm"
VM_BIN = Path(
    os.environ.get(
        "MNCS_VM_BIN",
        VM_RELEASE if VM_RELEASE.exists() else VM_REPO / "target" / "debug" / "mncs-vm",
    )
)
CACHE_DIR = Path(
    os.environ.get("MNCS_PROBE_CACHE_DIR", MAIN_ROOT / ".build" / "probe-cache")
)
MAX_AS_BYTES = int(
    os.environ.get("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", str(8 * 1024 * 1024 * 1024))
)


def _read_proc_vmhwm_bytes(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except OSError:
        pass
    return None


def _git_head(repo):
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _stage0_lock_rev():
    lock_path = ROOT / "mncs-language.lock.json"
    if lock_path.exists():
        data = json.loads(lock_path.read_text())
        return data.get("revision") or data.get("mncs_language_revision") or "unknown"
    return "unknown"


def _paged_args(source_bytes, stride=1024):
    chunks = [source_bytes[i:i + stride] for i in range(0, len(source_bytes), stride)] if source_bytes else []
    return [sem.pages_value(chunks), sem.integer(stride), sem.integer(len(source_bytes))]


def _byte_val(n):
    return {"byte": {"value": n}}


def _u64_val(n):
    return {"integer": {"value": n, "type": {"bits": 64, "signed": False}}}


def _u8_val(n):
    return {"integer": {"value": n, "type": {"bits": 8, "signed": False}}}


def _bool_val(b):
    return {"boolean": {"value": bool(b)}}


class NativeEmitterProbe:
    """Persistent Stage-0 host probe running MNCS-authored `mncs.compiler.vm_emit.v1`."""

    def __init__(self):
        env = dict(os.environ)
        env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{STDLIB_ROOT / 'library'}"
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
        env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.compiler.vm_emit.v1,mncs.std.text_utf8.v1"
        env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.vm_emit.v1"
        env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("project")
        env["MNCS_PROBE_BACKEND_ROOTS"] = (
            "compile_and_emit_vm_module,emit_verified_ssa,"
            "compile_and_emit_project_vm_module,emit_verified_project_ssa,"
            "emit_verified_project_ssa_with_sources,compile_project_for_emit"
        )
        env["MNCS_PROBE_CACHE_DIR"] = str(CACHE_DIR)
        env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(MAX_AS_BYTES)
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
            {
                "module": "mncs.compiler.vm_emit.v1",
                "function": "compile_and_emit_vm_module",
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            },
            {
                "module": "mncs.compiler.vm_emit.v1",
                "function": "emit_verified_ssa",
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            },
            {
                "module": "mncs.compiler.vm_emit.v1",
                "function": "compile_and_emit_project_vm_module",
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            },
            {
                "module": "mncs.compiler.vm_emit.v1",
                "function": "emit_verified_project_ssa_with_sources",
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            },
            {
                "module": "mncs.compiler.vm_emit.v1",
                "function": "compile_project_for_emit",
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            },
        ])
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [str(PROBE_BIN)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
        )
        self.peak_rss_bytes = None

    def send(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"native emitter probe terminated unexpectedly: {self.proc.poll()}"
        return json.loads(line)

    def record_types(self, module_name):
        return {r["name"]: r for r in self.send({"record_types": module_name})}

    def call_raw(self, function, args, type_arguments=None):
        req = {
            "schema_version": "0.1",
            "target": {"module": "mncs.compiler.vm_emit.v1", "function": function},
            "arguments": args,
            "type_arguments": (
                [sem.nat_arg(64), sem.nat_arg(1024)]
                if type_arguments is None
                else type_arguments
            ),
            "step_budget": 8000000,
        }
        resp = self.send(req)
        assert resp.get("status") == "returned", resp
        self.peak_rss_bytes = _read_proc_vmhwm_bytes(self.proc.pid) or self.peak_rss_bytes
        return resp["returned"][0]

    def call(self, function, args, type_arguments=None):
        return sem.decode(self.call_raw(function, args, type_arguments=type_arguments))

    def close(self):
        self.peak_rss_bytes = _read_proc_vmhwm_bytes(self.proc.pid) or self.peak_rss_bytes
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


class Stage0ModuleOracle:
    """Locked Stage-0 probe executing a target library module directly for differential comparison."""

    def __init__(self, module_name, library_paths):
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_LIBRARY_PATH"] = ":".join(str(p) for p in library_paths)
        env["MNCS_PROBE_MODULES"] = ""
        env["MNCS_PROBE_LIBRARY_MODULES"] = module_name
        env["MNCS_PROBE_EXECUTION_MODULES"] = module_name
        env["MNCS_PROBE_GENERIC_SEEDS"] = "[]"
        env["MNCS_PROBE_CACHE_DIR"] = str(CACHE_DIR)
        env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(MAX_AS_BYTES)
        self.module_name = module_name
        self.proc = subprocess.Popen(
            [str(PROBE_BIN)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
        )

    def call(self, function, args):
        req = {
            "schema_version": "0.1",
            "target": {"module": self.module_name, "function": function},
            "arguments": args,
            "type_arguments": [],
            "step_budget": 1000000,
        }
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"stage0 oracle probe terminated unexpectedly: {self.proc.poll()}"
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


class FreshVmSession:
    """Fresh `mncs-vm serve` process running an admitted `mncs.vm.artifact/1`."""

    def __init__(self, artifact_path):
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [str(VM_BIN), "serve", "--artifact", str(artifact_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ready_line = self.proc.stdout.readline()
        assert ready_line, f"mncs-vm serve died before ready: {self.proc.stderr.read()}"
        self.ready = json.loads(ready_line)
        assert self.ready.get("status") == "ready", self.ready
        self.req_id = 0
        self.peak_rss_bytes = None

    def call(self, module_name, function, args, *, type_arguments=None, expect_ok=True):
        self.req_id += 1
        req = {
            "schema_version": "mncs.vm.session/1",
            "id": self.req_id,
            "request": {
                "schema_version": "0.1",
                "target": {"module": module_name, "function": function},
                "arguments": args,
                "type_arguments": type_arguments or [],
                "step_budget": 1000000,
            },
        }
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"mncs-vm serve terminated: {self.proc.stderr.read()}"
        reply = json.loads(line)
        if not expect_ok:
            return reply
        assert reply.get("ok") is True, reply
        return reply["result"]

    def close(self):
        self.peak_rss_bytes = _read_proc_vmhwm_bytes(self.proc.pid)
        self.proc.stdin.close()
        rc = self.proc.wait(timeout=60)
        stderr = self.proc.stderr.read()
        assert rc == 0, f"mncs-vm serve rc={rc}: {stderr}"
        self.elapsed_s = time.monotonic() - self.t0


def _empty_invalid_ssa_summary(recs, valid_flag=False, fn_count=0, verified_count=0):
    empty_ident = {
        "record": {
            "type_identity": recs["SemanticIdentity"]["identity"],
            "name": "SemanticIdentity",
            "fields": [
                ["valid", {"boolean": {"value": False}}],
                ["len", sem.integer(0)],
                ["bytes", {"sequence": {"values": [{"byte": {"value": 0}}] * 512}}],
            ],
        }
    }
    return {
        "record": {
            "type_identity": recs["ValueSsaSummary"]["identity"],
            "name": "ValueSsaSummary",
            "fields": [
                [
                    "functions",
                    {
                        "finite": {
                            "type_identity": "mncs:0.2:finite-type:mncs.compiler.ssa.v1::FunctionList",
                            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.ssa.v1::FunctionList::FNil",
                            "discriminant": 0,
                            "payload": [],
                        }
                    },
                ],
                ["valid", {"boolean": {"value": valid_flag}}],
                ["function_count", sem.integer(fn_count)],
                ["supported_function_count", sem.integer(fn_count)],
                ["verified_function_count", sem.integer(verified_count)],
                ["value_count", sem.integer(0)],
                ["instruction_count", sem.integer(0)],
                ["block_count", sem.integer(0)],
                ["branch_count", sem.integer(0)],
                ["jump_count", sem.integer(0)],
                ["return_count", sem.integer(0)],
                ["failure_count", sem.integer(0)],
                ["switch_count", sem.integer(0)],
                ["block_parameter_count", sem.integer(0)],
                ["unsupported_block_count", sem.integer(0)],
                ["first_unsupported_function", sem.integer(0)],
                ["first_unsupported_block", sem.integer(0)],
                ["first_unsupported_kind", sem.integer(0)],
                ["call_count", sem.integer(0)],
                ["has_call", {"boolean": {"value": False}}],
                ["first_call_identity", empty_ident],
            ],
        }
    }


def _valid_dummy_header(parser_recs, mod_start, mod_end):
    return {
        "record": {
            "type_identity": parser_recs["Header"]["identity"],
            "name": "Header",
            "fields": [
                ["code", sem.integer(0)],
                ["start", sem.integer(0)],
                ["end", sem.integer(mod_end)],
                ["version_start", sem.integer(5)],
                ["version_end", sem.integer(9)],
                ["module_start", sem.integer(mod_start)],
                ["module_end", sem.integer(mod_end)],
                ["next_offset", sem.integer(mod_end + 1)],
                ["state", sem.integer(8)],
            ],
        }
    }


SCALAR_WITNESS_SOURCE = """\
mncs 0.18;
module mncs.test.native_vm_scalar_witness.v1;

fn mix_bits(a: u64, b: u64) -> (result: u64) {
    let x: u64 = (a & b) | (a ^ b);
    let y: u64 = (x >> 1) | (x << 1);
    return y;
}

fn arith_select(flag: bool, a: u64, b: u64) -> (result: u64) {
    let w: u64 = (a +% b) +% ((a / 3) +% (a % 3));
    let s: u64 = a -| b;
    let chosen: u64 = select(!flag, w, s);
    return chosen;
}

fn checked_u8_add(a: u8, b: u8) -> (result: u8) {
    return a + b;
}

fn caller_entry(a: byte, b: byte, flag: bool) -> (result: u64) {
    let wa: u64 = a as u64;
    let wb: u64 = b as u64;
    let mixed: u64 = mix_bits(wa, wb);
    return arith_select(flag, mixed, 7);
}
"""

UNSUPPORTED_RECORD_SOURCE = """\
mncs 0.18;
module mncs.test.unsupported_record.v1;
record Pair { a: u64, b: u64 }
fn make(a: u64, b: u64) -> (result: Pair) {
    return Pair { a: a, b: b };
}
"""

UNSUPPORTED_LOOP_SOURCE = """\
mncs 0.18;
module mncs.test.unsupported_loop.v1;
fn sum_small(n: u64) -> (result: u64) {
    iterate i up_to 8 carrying acc: u64 = 0 {
        if i >= n { return acc; }
        next acc = acc +% i;
    }
    return acc;
}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert PROBE_BIN.exists(), f"probe binary missing: {PROBE_BIN}"
    assert VM_BIN.exists(), f"mncs-vm binary missing: {VM_BIN}"

    utf8_src_path = STDLIB_ROOT / "library" / "std" / "utf8_primitives.mncs"
    utf8_src_bytes = utf8_src_path.read_bytes()
    utf8_src_sha256 = hashlib.sha256(utf8_src_bytes).hexdigest()
    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = _git_head(ROOT)
    vm_rev = _git_head(VM_REPO)
    stage0_rev = _stage0_lock_rev()

    with tempfile.TemporaryDirectory(prefix="mncs-native-vm-emit-") as tmpdir:
        tmp = Path(tmpdir)

        # 1. Emit `std/utf8_primitives.mncs` via native MNCS emitter (Process 1)
        t_emit1 = time.monotonic()
        p1 = NativeEmitterProbe()
        try:
            utf8_emit_1 = p1.call("compile_and_emit_vm_module", _paged_args(utf8_src_bytes))
            assert utf8_emit_1["ok"] is True, utf8_emit_1
            assert utf8_emit_1["function_count"] == 3, utf8_emit_1

            # Also emit broader scalar + direct-call witness
            witness_bytes = SCALAR_WITNESS_SOURCE.encode("utf-8")
            witness_emit = p1.call("compile_and_emit_vm_module", _paged_args(witness_bytes))
            assert witness_emit["ok"] is True, witness_emit
            assert witness_emit["function_count"] == 4, witness_emit

            recs = p1.record_types("mncs.compiler.vm_emit.v1")

            # Negative 1: unverified SSA (valid=False)
            unverified_res = p1.call(
                "emit_verified_ssa",
                _paged_args(utf8_src_bytes)
                + [
                    _empty_invalid_ssa_summary(recs, valid_flag=False, fn_count=0, verified_count=0),
                    _valid_dummy_header(recs, 19, 46),
                ],
            )
            assert unverified_res["ok"] is False and unverified_res["error_code"] == 1, unverified_res

            # Negative 2: unverified function count mismatch (valid=True, verified_function_count=0 != function_count=1)
            unverified_fn_res = p1.call(
                "emit_verified_ssa",
                _paged_args(utf8_src_bytes)
                + [
                    _empty_invalid_ssa_summary(recs, valid_flag=True, fn_count=1, verified_count=0),
                    _valid_dummy_header(recs, 19, 46),
                ],
            )
            assert unverified_fn_res["ok"] is False and unverified_fn_res["error_code"] == 1, unverified_fn_res

            # Negative 2b: real source failing declaration proof (type mismatch)
            bad_src = b"mncs 0.18; module mncs.test.bad.v1; fn bad(x: u64) -> (r: bool) { return x; }"
            bad_src_res = p1.call("compile_and_emit_vm_module", _paged_args(bad_src))
            assert bad_src_res["ok"] is False and bad_src_res["error_code"] == 1, bad_src_res

            # Negative 3: unsupported non-scalar type (record)
            rec_res = p1.call("compile_and_emit_vm_module", _paged_args(UNSUPPORTED_RECORD_SOURCE.encode("utf-8")))
            assert rec_res["ok"] is False and rec_res["error_code"] == 4, rec_res

            # Negative 4: unsupported bounded loop construct
            loop_res = p1.call("compile_and_emit_vm_module", _paged_args(UNSUPPORTED_LOOP_SOURCE.encode("utf-8")))
            assert loop_res["ok"] is False and loop_res["error_code"] == 5, loop_res
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t_emit1

        # 2. Independent Process 2 determinism check for `std/utf8_primitives.mncs`
        t_emit2 = time.monotonic()
        p2 = NativeEmitterProbe()
        try:
            utf8_emit_2 = p2.call("compile_and_emit_vm_module", _paged_args(utf8_src_bytes))
            assert utf8_emit_2["ok"] is True, utf8_emit_2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t_emit2

        utf8_artifact_1 = native_vm_artifact.package_native_vm_artifact(
            utf8_emit_1,
            utf8_src_bytes,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        utf8_artifact_2 = native_vm_artifact.package_native_vm_artifact(
            utf8_emit_2,
            utf8_src_bytes,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art1_bytes = json.dumps(utf8_artifact_1, separators=(",", ":")).encode("utf-8")
        art2_bytes = json.dumps(utf8_artifact_2, separators=(",", ":")).encode("utf-8")
        assert art1_bytes == art2_bytes, "native VM artifact emission must be bit-for-bit deterministic across processes"
        assert utf8_artifact_1["artifact_id"] == utf8_artifact_2["artifact_id"]
        utf8_artifact_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        utf8_artifact_path = tmp / "utf8_primitives.native.vm.json"
        utf8_artifact_path.write_bytes(art1_bytes)

        # 3. Independent `mncs-vm admit` check + tamper fail-closed check
        admit_ok = subprocess.run(
            [str(VM_BIN), "admit", "--artifact", str(utf8_artifact_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_ok.returncode == 0, f"mncs-vm admit failed: {admit_ok.stdout} {admit_ok.stderr}"
        admit_payload = json.loads(admit_ok.stdout)
        assert admit_payload["status"] == "admitted", admit_payload
        assert admit_payload["artifact_id"] == utf8_artifact_1["artifact_id"]

        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["assumptions"].append("tampered-after-sealing")
        tampered_path = tmp / "utf8_primitives.tampered.vm.json"
        tampered_path.write_text(json.dumps(tampered, separators=(",", ":")))
        admit_bad = subprocess.run(
            [str(VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0, f"tampered artifact was unexpectedly admitted: {admit_bad.stdout}"
        tamper_refusal = json.loads(admit_bad.stdout) if admit_bad.stdout.strip().startswith("{") else {"raw": admit_bad.stdout or admit_bad.stderr}
        assert tamper_refusal.get("status") == "refused", tamper_refusal

        # 4. Differential execution of `std/utf8_primitives.mncs` (672 calls) in fresh `mncs-vm` vs locked Stage-0
        utf8_cases = []
        for b in range(256):
            utf8_cases.append(("is_utf8_continuation", [_byte_val(b)]))
        for b in range(256):
            utf8_cases.append(("utf8_lead_need", [_byte_val(b)]))
        leads = [0, 127, 128, 193, 194, 223, 224, 225, 236, 237, 238, 240, 241, 244, 245, 255]
        conts = [0, 127, 128, 143, 144, 159, 160, 191, 192, 255]
        for lead in leads:
            for val in conts:
                utf8_cases.append(("utf8_first_continuation_valid", [_byte_val(lead), _byte_val(val)]))

        vm_sess = FreshVmSession(utf8_artifact_path)
        vm_max_memory_cells = 0
        vm_total_steps = 0
        vm_max_steps = 0
        vm_results = []
        try:
            for fn_name, call_args in utf8_cases:
                res = vm_sess.call("mncs.std.utf8_primitives.v1", fn_name, call_args)
                assert res["outcome"]["kind"] == "completed", (fn_name, call_args, res)
                usage = res["record"]["usage"]
                vm_max_memory_cells = max(vm_max_memory_cells, usage["max_memory_cells"])
                vm_total_steps += usage["steps"]
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_results.append((fn_name, call_args, res["execution"]["returned"]))
        finally:
            vm_sess.close()

        oracle = Stage0ModuleOracle("mncs.std.utf8_primitives.v1", [STDLIB_ROOT / "library"])
        try:
            for fn_name, call_args, vm_ret in vm_results:
                s0 = oracle.call(fn_name, call_args)
                assert s0["status"] == "returned", (fn_name, call_args, s0)
                assert s0["returned"] == vm_ret, f"MISMATCH {fn_name}({call_args}): vm={vm_ret} stage0={s0['returned']}"
        finally:
            oracle.close()

        # 5. Broader scalar + direct-call + checked-overflow witness vs locked Stage-0
        witness_artifact = native_vm_artifact.package_native_vm_artifact(
            witness_emit,
            witness_bytes,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        witness_artifact_path = tmp / "scalar_witness.native.vm.json"
        witness_artifact_path.write_text(json.dumps(witness_artifact, separators=(",", ":")))

        witness_lib_dir = tmp / "witness_lib" / "test"
        witness_lib_dir.mkdir(parents=True)
        (witness_lib_dir / "native_vm_scalar_witness.mncs").write_bytes(witness_bytes)

        witness_cases = [
            ("mix_bits", [_u64_val(0xAA), _u64_val(0x55)], "completed"),
            ("mix_bits", [_u64_val(0x81), _u64_val(0x0F)], "completed"),
            ("arith_select", [_bool_val(False), _u64_val(10), _u64_val(3)], "completed"),
            ("arith_select", [_bool_val(True), _u64_val(10), _u64_val(30)], "completed"),
            ("arith_select", [_bool_val(False), _u64_val((1 << 64) - 1), _u64_val(5)], "completed"),
            ("caller_entry", [_byte_val(0xC3), _byte_val(0x3C), _bool_val(False)], "completed"),
            ("caller_entry", [_byte_val(0xC3), _byte_val(0x3C), _bool_val(True)], "completed"),
            ("checked_u8_add", [_u8_val(100), _u8_val(50)], "completed"),
            ("checked_u8_add", [_u8_val(250), _u8_val(10)], "program_failure"),
        ]

        witness_vm = FreshVmSession(witness_artifact_path)
        witness_vm_max_cells = 0
        witness_vm_results = []
        try:
            for fn_name, call_args, expected_kind in witness_cases:
                res = witness_vm.call("mncs.test.native_vm_scalar_witness.v1", fn_name, call_args)
                kind = res["outcome"]["kind"]
                assert kind == expected_kind, (fn_name, call_args, expected_kind, res)
                witness_vm_max_cells = max(witness_vm_max_cells, res["record"]["usage"]["max_memory_cells"])
                witness_vm_results.append((fn_name, call_args, kind, res["execution"]["returned"]))
        finally:
            witness_vm.close()

        witness_oracle = Stage0ModuleOracle("mncs.test.native_vm_scalar_witness.v1", [tmp / "witness_lib"])
        try:
            for fn_name, call_args, kind, vm_ret in witness_vm_results:
                s0 = witness_oracle.call(fn_name, call_args)
                if kind == "completed":
                    assert s0["status"] == "returned", (fn_name, call_args, s0)
                    assert s0["returned"] == vm_ret, f"MISMATCH {fn_name}({call_args}): vm={vm_ret} stage0={s0['returned']}"
                else:
                    assert s0["status"] != "returned", f"Stage-0 did not trap on {fn_name}({call_args}): {s0}"
        finally:
            witness_oracle.close()

        summary = {
            "status": "passed",
            "provenance": {
                "executable_program_producer": "mncs.compiler.vm_emit.v1::compile_and_emit_vm_module",
                "native_ssa_producer": "mncs.compiler.ssa.v1::lower_flow_unit_value_ssa",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(VM_BIN),
                "host_probe_execution_lock": stage0_rev,
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
            },
            "utf8_primitives": {
                "source_path": str(utf8_src_path),
                "source_sha256": utf8_src_sha256,
                "module": "mncs.std.utf8_primitives.v1",
                "function_count": utf8_emit_1["function_count"],
                "block_count": utf8_emit_1["block_count"],
                "instruction_count": utf8_emit_1["instruction_count"],
                "artifact_id": utf8_artifact_1["artifact_id"],
                "artifact_sha256": utf8_artifact_sha256,
                "artifact_bytes": len(art1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_wall_s": round(emit1_wall_s, 3),
                "emit_process1_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_wall_s": round(emit2_wall_s, 3),
                "emit_process2_peak_rss_bytes": p2.peak_rss_bytes,
                "vm_admission_status": admit_payload["status"],
                "vm_differential_cases": len(utf8_cases),
                "vm_differential_matches": len(utf8_cases),
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_memory_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "scalar_call_witness": {
                "module": "mncs.test.native_vm_scalar_witness.v1",
                "function_count": witness_emit["function_count"],
                "block_count": witness_emit["block_count"],
                "instruction_count": witness_emit["instruction_count"],
                "artifact_id": witness_artifact["artifact_id"],
                "differential_cases": len(witness_cases),
                "differential_matches": len(witness_cases),
                "checked_overflow_trap_matched": True,
                "vm_resident_peak_rss_bytes": witness_vm.peak_rss_bytes,
                "vm_max_memory_cells": witness_vm_max_cells,
            },
            "negative_rejections": {
                "unverified_ssa_invalid_summary": {"ok": unverified_res["ok"], "error_code": unverified_res["error_code"]},
                "unverified_ssa_count_mismatch": {"ok": unverified_fn_res["ok"], "error_code": unverified_fn_res["error_code"]},
                "unverified_source_type_error": {"ok": bad_src_res["ok"], "error_code": bad_src_res["error_code"]},
                "unsupported_record_type": {"ok": rec_res["ok"], "error_code": rec_res["error_code"]},
                "unsupported_bounded_loop": {"ok": loop_res["ok"], "error_code": loop_res["error_code"]},
                "tampered_artifact_vm_admission": {"status": tamper_refusal.get("status"), "returncode": admit_bad.returncode},
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
