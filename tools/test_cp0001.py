#!/usr/bin/env python3
"""CP-0001: MNCS-native logical immutable source over bounded pages.

The host transports page bytes only. Every offset, span, token, header, and
identity decision executes in MNCS (source/lexer/segment/parser/kernel
logical entry points) or in the Stage-0 oracle for differential comparison.
Python never tokenizes, parses, hashes content, or derives line/column facts,
except byte-level transport checks (reassembly, ASCII range) and the
spec-transcribed `ref_line_col` shared with test_frontend.py.

Tiers (MNCS_CP0001_TIERS, default ABCD; MNCS_CP0001_SMOKE=1 for a fast cut):
  A  strided correctness on small inputs (full-token equality vs oracle,
     batch/single equivalence, cross-stride fingerprint stability);
  B  1-4KB boundary grid (exact page fills, off-by-ones, straddling tokens,
     EOF-on-boundary, two-page splits at every position);
  C  malformed compositions (every validation code), transport faults
     (dup/missing/order/forgery via fingerprint), overlong/trivia-prefix
     budgets, header fuel, line/column rendering;
  D  self-host milestones (real compiler/CLI sources to ~490KB) plus the
     self-host-distance matrix with per-stage booleans from actual execution.

Evidence: evidence/cp0001-results.json (compact) and
evidence/cp0001-matrix.json (executable matrix input). No full token traces
are retained; digests plus the deterministic suite reproduce them.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR",
                                        ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
OUT = ROOT / ".build"
OUT.mkdir(exist_ok=True)

PAGE_BOUND = 1024
STRIDE_BOUND = 1024
SMALL_BOUND = 256
SMOKE = os.environ.get("MNCS_CP0001_SMOKE") == "1"
TIERS = os.environ.get("MNCS_CP0001_TIERS", "A" if SMOKE else "ABCD")
FNV_BASIS = 14695981039346656037


def nat_arg(value):
    return {"kind": "nat", "value": value}


def integer(n):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": n}}


def boolean(v):
    return {"boolean": {"value": bool(v)}}


def byte_sequence(raw):
    return {"sequence": {"values": [{"byte": {"value": value}} for value in raw]}}


def pages_value(chunks):
    return {"sequence": {"values": [byte_sequence(chunk) for chunk in chunks]}}


def chunk(data, stride):
    assert stride >= 1
    return [data[i:i + stride] for i in range(0, len(data), stride)] or []


def reassemble(chunks):
    return b"".join(chunks)


def decode(value):
    if "record" in value:
        return {key: decode(item) for key, item in value["record"]["fields"]}
    if "finite" in value:
        finite = value["finite"]
        return {"$v": finite["discriminant"],
                "$p": {key: decode(item) for key, item in finite.get("payload", [])}}
    if "sequence" in value:
        return [decode(item) for item in value["sequence"]["values"]]
    if "boolean" in value:
        return value["boolean"]["value"]
    return next(iter(value.values()))["value"]


def flist(value, nil=0, cons=1):
    items = []
    while value["$v"] == cons:
        items.append(value["$p"]["head"])
        value = value["$p"]["tail"]
    assert value["$v"] == nil, value
    return items


def wire_token_list(value):
    """Iterative TokenList decode: batch spines reach 1024 cons cells,
    past Python's recursion limit for the shared recursive decoder."""
    items = []
    while True:
        finite = value["finite"]
        if finite["discriminant"] == 0:
            return items
        assert finite["discriminant"] == 1, finite
        payload = {key: item for key, item in finite.get("payload", [])}
        items.append(decode(payload["head"]))
        value = payload["tail"]


def ref_line_col(data, offset):
    """Spec transcription of SourceSpan::at (ASCII domain), shared with
    test_frontend.py: 1-based line from newline count, column from bytes
    since the last newline."""
    offset = min(offset, len(data))
    line = data.count(b"\n", 0, offset) + 1
    line_start = data.rfind(b"\n", 0, offset) + 1
    return (line, offset - line_start + 1)


class Probe:
    def __init__(self, modules, execution_modules, seeds, with_project=False):
        env = dict(os.environ)
        reference_interpreter = env.get("MNCS_PROBE_BACKEND") == "reference_interpreter"
        if reference_interpreter:
            env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = modules
        env["MNCS_PROBE_EXECUTION_MODULES"] = execution_modules
        if not reference_interpreter:
            env.setdefault("MNCS_PROBE_BACKEND", "cranelift")
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(seeds)
        self.proc = subprocess.Popen(
            [env.get("MNCS_PROBE_BIN",
                     str(BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.count = 0
        self.steps = []
        self.digest = hashlib.sha256()
        self.with_project = with_project

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"probe terminated: {self.proc.poll()}"
        return json.loads(line)

    def run_global_wire(self, module, function, pages, stride, total,
                        extra=(), budget=8_000_000):
        request = {
            "schema_version": "0.1",
            "target": {"module": module, "function": function},
            "arguments": [pages_value(pages), integer(stride), integer(total),
                          *[a for a in extra]],
            "type_arguments": [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)],
            "step_budget": budget,
        }
        result = self.send(request)
        assert result["status"] == "returned", (function, result)
        self.count += 1
        self.steps.append(result["steps"])
        semantic = [module, function, stride, total,
                    hashlib.sha256(reassemble(pages)).hexdigest(),
                    [extra_sem(a) for a in extra],
                    result["status"], result["returned"], result["failure"],
                    result["steps"]]
        self.digest.update(json.dumps(semantic, sort_keys=True).encode())
        return result

    def run_global(self, module, function, pages, stride, total, extra=(),
                   budget=8_000_000):
        return decode(self.run_global_wire(
            module, function, pages, stride, total, extra, budget
        )["returned"][0])

    def run_single(self, module, function, data, extra=(), bound=SMALL_BOUND,
                   budget=8_000_000):
        request = {
            "schema_version": "0.1",
            "target": {"module": module, "function": function},
            "arguments": [byte_sequence(data), *[a for a in extra]],
            "type_arguments": [nat_arg(bound)],
            "step_budget": budget,
        }
        result = self.send(request)
        assert result["status"] == "returned", (function, result)
        self.count += 1
        self.steps.append(result["steps"])
        semantic = [module, function, bound,
                    hashlib.sha256(bytes(data)).hexdigest(),
                    [extra_sem(a) for a in extra],
                    result["status"], result["returned"], result["failure"],
                    result["steps"]]
        self.digest.update(json.dumps(semantic, sort_keys=True).encode())
        return decode(result["returned"][0])

    def run_plain(self, module, function, args, budget=8_000_000):
        request = {
            "schema_version": "0.1",
            "target": {"module": module, "function": function},
            "arguments": list(args),
            "type_arguments": [],
            "step_budget": budget,
        }
        result = self.send(request)
        assert result["status"] == "returned", (function, result)
        self.count += 1
        self.steps.append(result["steps"])
        self.digest.update(json.dumps(
            [module, function, "plain", result["returned"], result["steps"]],
            sort_keys=True).encode())
        return decode(result["returned"][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=120) == 0


def extra_sem(arg):
    if isinstance(arg, dict) and "integer" in arg:
        return arg["integer"]["value"]
    if isinstance(arg, dict) and "boolean" in arg:
        return arg["boolean"]["value"]
    return hashlib.sha256(json.dumps(arg, sort_keys=True).encode()).hexdigest()


def lex_probe():
    seeds = []
    for module, functions in {
        "mncs.compiler.source.v1": ["validate_pages", "byte_at_global",
                                     "ascii_global", "fingerprint_global",
                                     "line_col_global"],
        "mncs.compiler.segment.v1": ["byte_at_global", "next_token_global",
                                      "significant_global", "lex_batch_from"],
        "mncs.compiler.kernel.v1": ["parse_header_global", "lex_step_global"],
    }.items():
        for function in functions:
            seeds.append({"module": module, "function": function,
                          "type_arguments": [nat_arg(PAGE_BOUND),
                                             nat_arg(STRIDE_BOUND)]})
    seeds.append({"module": "mncs.compiler.segment.v1",
                  "function": "next_token",
                  "type_arguments": [nat_arg(SMALL_BOUND)]})
    seeds.append({"module": "mncs.compiler.kernel.v1",
                  "function": "lex_summary",
                  "type_arguments": [nat_arg(SMALL_BOUND)]})
    return Probe("source,lexer,parser,segment,kernel",
                 "mncs.compiler.source.v1,mncs.compiler.segment.v1,"
                 "mncs.compiler.kernel.v1",
                 seeds)


def full_probe():
    # Project-only execution module: this probe issues compile_project
    # (plus oracles/record types) and never calls ssa.v1 directly, so it
    # needs no ssa session. Scoping also avoids the backend dual-session
    # finalize failure (see the split Probe in tools/test_project.py):
    # listing ssa.v1 here previously retained only 1 of 2 sessions.
    seeds = [
        {"module": "mncs.compiler.project.v1", "function": "compile_project",
         "type_arguments": [nat_arg(1024), nat_arg(1024)]},
    ]
    return Probe("source,lexer,parser,segment,decl,flow,ssa,project",
                 "mncs.compiler.project.v1",
                 seeds, with_project=True)


def single_walk(probe, pages, stride, total):
    """Host-driven single-token walk to EOF. Every boundary/kind/span
    decision is an MNCS fact; the host only carries the cursor."""
    tokens = []
    cursor = 0
    while True:
        token = probe.run_global("mncs.compiler.segment.v1",
                                 "next_token_global", pages, stride, total,
                                 (integer(cursor),))
        tokens.append(token)
        if token["kind"] == 0 or token["diagnostic"] == 5:
            return tokens
        assert token["end"] > cursor, (token, cursor)
        cursor = token["end"]
        assert len(tokens) <= total + 2, "walk did not terminate"


def batch_walk(probe, pages, stride, total, budget=8_000_000):
    tokens = []
    cursor = 0
    while True:
        wire = probe.run_global_wire("mncs.compiler.segment.v1",
                                     "lex_batch_from", pages, stride, total,
                                     (integer(cursor),), budget=budget)
        returned = wire["returned"][0]
        fields = {key: item for key, item in
                  returned["record"]["fields"]}
        items = wire_token_list(fields["tokens"])
        batch = {"tokens": items, "next": decode(fields["next"]),
                 "eof": decode(fields["eof"]), "count": decode(fields["count"])}
        assert batch["count"] == len(items), batch["count"]
        tokens.extend(items)
        if batch["eof"] or not items or batch["next"] == cursor \
                or items[-1].get("diagnostic") == 5:
            return tokens, batch
        assert batch["next"] > cursor, batch
        cursor = batch["next"]
        assert len(tokens) <= total + 2, "batch walk did not terminate"


def significant_walk(probe, pages, stride, total):
    tokens = []
    cursor = 0
    while True:
        token = probe.run_global("mncs.compiler.segment.v1",
                                 "significant_global", pages, stride, total,
                                 (integer(cursor),))
        tokens.append(token)
        if token["kind"] == 0 or token["diagnostic"] == 5:
            return tokens
        if is_trivia_native(token):
            break  # fuel-exhausted clean trivia; host recalls (as native)
        assert token["end"] > cursor, (token, cursor)
        cursor = token["end"]
        assert len(tokens) <= total + 2, "significant walk did not terminate"
    return tokens


def oracle_tokens(probe, kinds_inv, data):
    oracle = probe.send({"oracle": data.decode("ascii")})["lexical"]
    diagnostics = {(item["span"]["start"], item["span"]["end"]): item["code"]
                   for item in oracle["diagnostics"]}
    expected = []
    for item in oracle["tokens"]:
        span = item["span"]
        code = diagnostics.get((span["start"], span["end"]))
        expected.append((kinds_inv[item["kind"]], span["start"], span["end"],
                         {"MNL001": 1, "MNL002": 2}.get(code, 0)))
    return expected, oracle["diagnostics"]


def oracle_tokens_raw(probe, text):
    """Oracle tokens with kind strings preserved, so tier D can classify
    known divergences (CP-0022 `not`) instead of crashing on them."""
    oracle = probe.send({"oracle": text})["lexical"]
    diagnostics = {(item["span"]["start"], item["span"]["end"]): item["code"]
                   for item in oracle["diagnostics"]}
    expected = []
    for item in oracle["tokens"]:
        span = item["span"]
        code = diagnostics.get((span["start"], span["end"]))
        expected.append((item["kind"], span["start"], span["end"],
                         {"MNL001": 1, "MNL002": 2}.get(code, 0)))
    return expected


def compare_modulo_not(joined, expected, data, kinds_inv):
    """Strict pairwise comparison modulo the classified CP-0022 pair:
    native (7, s, e, 2) on a single `!` byte equals oracle ('not', s, e, 0),
    the deliberate version-neutral-scanner divergence (decl.mncs reinterprets
    that exact byte as prefix negation past the 0.13 gate). Returns
    (exact_ok, gap_count, first_gap_span). Any other mismatch — including any
    other unknown oracle kind — raises: that would be a new defect, not a
    classified gap."""
    gaps = 0
    first_gap = None
    exact = len(joined) == len(expected)
    for index, (native, oracle_tok) in enumerate(zip(joined, expected)):
        kind, start, end, diag = oracle_tok
        if kind == "not":
            assert diag == 0, ("not with diagnostic", index, oracle_tok)
            assert native == (7, start, end, 2) and data[start:end] == b"!", \
                ("not-pair mismatch", index, native, oracle_tok)
            gaps += 1
            exact = False
            if first_gap is None:
                first_gap = [start, end]
            continue
        assert kind in kinds_inv, \
            ("unclassified oracle kind", index, oracle_tok)
        if native != (kinds_inv[kind], start, end, diag):
            raise AssertionError(("token mismatch", index, native, oracle_tok))
    assert len(joined) == len(expected), (len(joined), len(expected))
    return exact, gaps, first_gap


def is_trivia_native(token):
    return token["kind"] in (1, 2, 3) and token["diagnostic"] in (0, 6)


def check_coverage(tokens, total):
    cursor = 0
    for token in tokens:
        assert token["start"] == cursor, (token, cursor)
        cursor = token["end"]
    assert cursor == total, (cursor, total)


def trivia_bytes(tokens, total):
    covered = bytearray(total)
    for token in tokens:
        if is_trivia_native(token):
            for i in range(token["start"], token["end"]):
                covered[i] = 1
    return bytes(covered)


def oracle_trivia_bytes(expected, total):
    covered = bytearray(total)
    for kind, start, end, diag in expected:
        if kind in (1, 2, 3) and diag == 0:
            for i in range(start, min(end, total)):
                covered[i] = 1
    return bytes(covered)


FIXED_SAMPLES = [
    b"",
    b"a",
    b" ",
    b"mncs 0.18; module a;",
    b"mncs 0.18; module a.b;",
    b"/*c*/",
    b"/* /* */",
    b"/* unterminated",
    b"// line\nfn f() -> (r: u64) { return 1; }",
    b"-> => && || == != <= >= << >> .. +% -% *% +| -| *|",
    b"+ - * / % & | ^ < > = . : ; , ( ) { } [ ]",
    b"0 07 1.2 3..4 5.6.7",
    b"fn f(a: u64, b: u64) -> (r: u64) { let x: u64 = a + b * 2; return x; }",
    b"a" * 64,
    b"ab " * 40,
    b" " * 200,
    b"x" * 256,
]

SMALL_STRIDES = [1, 2, 3, 5, 7, 8, 13, 16, 31, 32, 33, 63, 64, 65, 127, 128,
                 255, 256]
NONASCII = [b"mncs 0.18; module caf\xc3\xa9;", b"\xff", b"a\x80b"]


def tier_a(probe, kinds_inv, stats):
    samples = FIXED_SAMPLES if not SMOKE else FIXED_SAMPLES[:6]
    strides = SMALL_STRIDES if not SMOKE else [1, 7, 64]
    rng = random.Random(257)
    alphabet = "ab_09. +-*/%|&^<>=!;:,()\t\n{}fmns"
    fuzz = [ "".join(rng.choice(alphabet)
                     for _ in range(rng.randrange(1, SMALL_BOUND + 1))).encode()
             for _ in range(0 if SMOKE else 24)]
    cases = 0
    for data in samples + fuzz:
        assert all(byte < 128 for byte in data)
        expected, _ = oracle_tokens(probe, kinds_inv, data)
        fingerprints = set()
        for stride in strides:
            pages = chunk(data, stride)
            if len(pages) > PAGE_BOUND:
                stats["skipped_page_bound"] += 1
                continue
            total = len(data)
            assert reassemble(pages) == data
            stats["transport_bytes"] += total
            code = probe.run_global("mncs.compiler.source.v1",
                                    "validate_pages", pages, stride, total)
            assert code == 0, (data, stride, code)
            assert probe.run_global("mncs.compiler.source.v1", "ascii_global",
                                    pages, stride, total) is True
            fp = probe.run_global("mncs.compiler.source.v1",
                                  "fingerprint_global", pages, stride, total)
            fingerprints.add(fp)
            if total == 0:
                assert fp == FNV_BASIS, fp
            for offset in {0, total, total + 5, 2 ** 64 - 1} | \
                    {s for s in range(0, total, 37)}:
                want = data[offset] if offset < total else 256
                got = probe.run_global("mncs.compiler.source.v1",
                                       "byte_at_global", pages, stride, total,
                                       (integer(offset),))
                assert got == want, (data, stride, offset, got, want)
            for start, end, want in [(0, total, True), (total, total, True),
                                     (1, 0, False), (0, total + 1, False)]:
                got_span = probe.run_plain(
                    "mncs.compiler.segment.v1", "span_valid_global",
                    [integer(total), integer(start), integer(end)])
                assert got_span is want, (data, stride, start, end)
            native = single_walk(probe, pages, stride, total)
            observed = [(t["kind"], t["start"], t["end"], t["diagnostic"])
                        for t in native]
            assert 5 not in [t["diagnostic"] for t in native], (data, stride)
            assert 6 not in [t["diagnostic"] for t in native], (data, stride)
            assert observed == expected + [(0, total, total, 0)], \
                (data, stride, observed, expected)
            check_coverage(native[:-1], total)
            batched, _ = batch_walk(probe, pages, stride, total)
            assert [(t["kind"], t["start"], t["end"], t["diagnostic"])
                    for t in batched] == observed, (data, stride)
            significant = significant_walk(probe, pages, stride, total)
            want_sig = [t for t in expected
                        if not (t[0] in (1, 2, 3) and t[3] == 0)]
            assert [(t["kind"], t["start"], t["end"], t["diagnostic"])
                    for t in significant] == \
                want_sig + [(0, total, total, 0)], (data, stride)
            for offset in {0, total} | {t[1] for t in expected[:8]}:
                rendered = probe.run_global("mncs.compiler.source.v1",
                                            "line_col_global", pages, stride,
                                            total, (integer(offset),))
                assert (rendered["line"], rendered["col"]) == \
                    ref_line_col(data, offset), (data, stride, offset)
            cases += 1
            stats["tokens_compared"] += len(expected)
        assert len(fingerprints) == 1, (data, fingerprints)
        stats["fingerprint_shapes"] += 1
    # Single-view triple equivalence on a subset: the same bytes lex
    # identically through one bounded view and through strided pages.
    for data in (FIXED_SAMPLES[3:7] if not SMOKE else FIXED_SAMPLES[3:4]):
        expected, _ = oracle_tokens(probe, kinds_inv, data)
        cursor, seen = 0, []
        while True:
            token = probe.run_single("mncs.compiler.segment.v1", "next_token",
                                     data, (integer(cursor),))
            seen.append((token["kind"], token["start"], token["end"],
                         token["diagnostic"]))
            if token["kind"] == 0:
                break
            cursor = token["end"]
        assert seen == expected + [(0, len(data), len(data), 0)], data
        summary = probe.run_single("mncs.compiler.kernel.v1", "lex_summary",
                                   data)
        stepped = {"code": 0, "cursor": 0, "tokens": 0, "trivia": 0,
                   "errors": 0, "first_start": 0, "first_end": 0}
        pages = chunk(data, 7)
        evidence_id = probe.send(
            {"record_types": "mncs.compiler.kernel.v1"})
        # Host-driven accumulation must equal the whole-source summary.
        while True:
            stepped = kernel_step(probe, evidence_id, pages, 7, len(data),
                                  stepped)
            if stepped["cursor"] == len(data):
                break
        assert stepped == summary, (data, stepped, summary)
        stats["summary_equivalence"] += 1
    for data in NONASCII[:3 if not SMOKE else 1]:
        pages = chunk(data, 7)
        total = len(data)
        assert probe.run_global("mncs.compiler.source.v1", "validate_pages",
                                pages, 7, total) == 0
        assert probe.run_global("mncs.compiler.source.v1", "ascii_global",
                                pages, 7, total) is False
        first_bad = next(i for i, byte in enumerate(data) if byte >= 128)
        token = probe.run_global("mncs.compiler.segment.v1",
                                 "next_token_global", pages, 7, total,
                                 (integer(first_bad),))
        assert (token["kind"], token["start"], token["end"],
                token["diagnostic"]) == (7, first_bad, first_bad + 1, 3)
        header = probe.run_global("mncs.compiler.kernel.v1",
                                  "parse_header_global", pages, 7, total)
        assert header["code"] == 8 and header["end"] == total, header
        stats["nonascii"] += 1
    stats["tier_a_cases"] = cases


def kernel_step(probe, evidence_id, pages, stride, total, stepped):
    identities = {record["name"]: record for record in evidence_id}
    info = identities["LexicalEvidence"]
    wire = {"record": {"type_identity": info["identity"],
                       "name": "LexicalEvidence",
                       "fields": [[key, integer(stepped[key])]
                                  for key in ("code", "cursor", "tokens",
                                              "trivia", "errors",
                                              "first_start", "first_end")]}}
    request = {
        "schema_version": "0.1",
        "target": {"module": "mncs.compiler.kernel.v1",
                   "function": "lex_step_global"},
        "arguments": [pages_value(pages), integer(stride), integer(total),
                      wire],
        "type_arguments": [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)],
        "step_budget": 8_000_000,
    }
    result = probe.send(request)
    assert result["status"] == "returned", result
    probe.count += 1
    probe.steps.append(result["steps"])
    probe.digest.update(json.dumps(
        ["lex_step_global", stride, total, stepped, result["returned"],
         result["steps"]], sort_keys=True).encode())
    return decode(result["returned"][0])


SOUP = (b"fn f01() -> (r: u64) { return 42; } // tail\n"
        b"/* block c */ let xyz: u64 = 1.2; while01 match next over as up_to "
        b"carrying reuse_99_zzz 0.18 3..4 ")


def tier_b(probe, kinds_inv, stats):
    lengths = [0, 1, 2, 3, 63, 64, 65, 127, 128, 129, 255, 256, 257, 511,
               512, 513, 1023, 1024, 1025, 2048, 2049, 3072, 4096, 4097]
    strides = [7, 64, 256, 1024] if not SMOKE else [7, 64]
    if not SMOKE:
        strides = [1] + strides
    cases = 0
    for length in lengths:
        if SMOKE and length > 257:
            continue
        data = (SOUP * ((length // len(SOUP)) + 1))[:length]
        expected, _ = oracle_tokens(probe, kinds_inv, data)
        assert max((end - start for _, start, end, _ in expected),
                   default=0) < 8192
        for stride in strides:
            pages = chunk(data, stride)
            if len(pages) > PAGE_BOUND:
                stats["skipped_page_bound"] += 1
                continue
            total = len(data)
            assert reassemble(pages) == data
            stats["transport_bytes"] += total
            code = probe.run_global("mncs.compiler.source.v1",
                                    "validate_pages", pages, stride, total)
            assert code == 0, (length, stride, code)
            batched, batch = batch_walk(probe, pages, stride, total)
            observed = [(t["kind"], t["start"], t["end"], t["diagnostic"])
                        for t in batched]
            assert 5 not in [t["diagnostic"] for t in batched], (length, stride)
            assert 6 not in [t["diagnostic"] for t in batched], (length, stride)
            assert observed == expected + [(0, total, total, 0)], \
                (length, stride, len(observed), len(expected))
            assert batch["eof"] is True and batch["next"] == total
            check_coverage(batched[:-1], total)
            assert trivia_bytes(batched, total) == \
                oracle_trivia_bytes(expected, total)
            if total <= 1024:
                native = single_walk(probe, pages, stride, total)
                assert [(t["kind"], t["start"], t["end"], t["diagnostic"])
                        for t in native] == observed, (length, stride)
            else:
                mid = observed[len(observed) // 2][1]
                for cursor in {0, mid, total, total + 1}:
                    token = probe.run_global(
                        "mncs.compiler.segment.v1", "next_token_global",
                        pages, stride, total, (integer(cursor),))
                    if cursor > total:
                        assert (token["kind"], token["diagnostic"]) == (0, 4)
                    elif cursor == total:
                        assert (token["kind"], token["start"],
                                token["diagnostic"]) == (0, total, 0)
                    else:
                        match = next(t for t in observed if t[1] == cursor)
                        assert (token["kind"], token["start"], token["end"],
                                token["diagnostic"]) == match, (length, stride)
            eof = probe.run_global("mncs.compiler.segment.v1",
                                   "next_token_global", pages, stride, total,
                                   (integer(total),))
            assert (eof["kind"], eof["start"], eof["end"],
                    eof["diagnostic"]) == (0, total, total, 0)
            cases += 1
            stats["tokens_compared"] += len(expected)
    stats["tier_b_cases"] = cases
    # Two-page splits at every position: canonical halves lex identically,
    # non-canonical halves are rejected with a precise code.
    for data in ([b"mncs 0.18; module a;", SOUP[:64]]
                 if not SMOKE else [b"mncs 0.18; module a;"]):
        expected, _ = oracle_tokens(probe, kinds_inv, data)
        total = len(data)
        for k in range(1, total):
            pages = [data[:k], data[k:]]
            stride = k
            code = probe.run_global("mncs.compiler.source.v1",
                                    "validate_pages", pages, stride, total)
            canonical = (total - k) <= k and (total + k - 1) // k == 2
            if canonical:
                assert code == 0, (k, code)
                native = single_walk(probe, pages, stride, total)
                assert [(t["kind"], t["start"], t["end"], t["diagnostic"])
                        for t in native] == expected + [(0, total, total, 0)], k
                stats["splits_canonical"] += 1
            else:
                assert code in (4, 5), (k, code)
                stats["splits_rejected"] += 1


def tier_c(probe, kinds_inv, stats):
    src = "mncs.compiler.source.v1"
    seg = "mncs.compiler.segment.v1"
    ker = "mncs.compiler.kernel.v1"
    data = b"mncs 0.18; module a; fn f() -> (r: u64) { return 7; }"
    total = len(data)
    good = chunk(data, 16)
    good_fp = probe.run_global(src, "fingerprint_global", good, 16, total)
    malformed = [
        ("stride-zero", [data], 0, total, 1),
        ("stride-huge", [data], 1025, total, 2),
        ("page-over-stride",
         [data[:16], data[16:32], data[:20], b""], 16, total, 3),
        ("short-middle", [data[:16], data[16:20], data[20:32], data[32:]],
         16, total, 4),
        ("empty-middle", [data[:16], b"", data[16:32], data[32:]], 16, total, 4),
        ("extra-page", good + [b"padding"], 16, total, 5),
        ("missing-page", good[:-1], 16, total, 5),
        ("total-points-elsewhere", good, 16, 15, 5),
        ("total-off-by-one", good, 16, total - 1, 6),
        ("terminal-mismatch",
         [data[:8], data[8:16], data[16:24], data[24:32], data[32:39]],
         8, 40, 6),
        ("empty-noncanonical", [b""], 16, 0, 5),
    ]
    for name, pages, stride, claimed, want in malformed:
        code = probe.run_global(src, "validate_pages", pages, stride, claimed)
        assert code == want, (name, code, want)
        stats["malformed"][name] = code
    # Empty source canonical form: zero pages, zero total.
    assert probe.run_global(src, "validate_pages", [], 16, 0) == 0
    assert probe.run_global(src, "ascii_global", [], 16, 0) is True
    assert probe.run_global(src, "fingerprint_global", [], 16, 0) == FNV_BASIS
    eof = probe.run_global(seg, "next_token_global", [], 16, 0, (integer(0),))
    assert (eof["kind"], eof["start"], eof["end"],
            eof["diagnostic"]) == (0, 0, 0, 0)
    header = probe.run_global(ker, "parse_header_global", [], 16, 0)
    assert header["code"] == 2, header  # EOF where `mncs` belongs
    # Transport faults that preserve canonical shape change identity.
    swapped = [good[1], good[0]] + good[2:]
    assert probe.run_global(src, "validate_pages", swapped, 16, total) == 0
    assert probe.run_global(src, "fingerprint_global", swapped, 16, total) \
        != good_fp
    duped = [good[0], good[0]] + good[2:]
    assert probe.run_global(src, "validate_pages", duped, 16, total) == 0
    assert probe.run_global(src, "fingerprint_global", duped, 16, total) \
        != good_fp
    stats["transport_faults"] = 2
    # Oversized indexes are total: sentinel bytes, invalid-cursor tokens.
    for offset in (total, total + 5, 2 ** 64 - 1):
        assert probe.run_global(src, "byte_at_global", good, 16, total,
                                (integer(offset),)) == 256
    bad = probe.run_global(seg, "next_token_global", good, 16, total,
                           (integer(total + 1),))
    assert (bad["kind"], bad["start"], bad["end"],
            bad["diagnostic"]) == (0, total, total, 4)
    assert probe.run_plain(seg, "span_valid_global",
                           [integer(total), integer(5), integer(3)]) is False
    # Trivia-prefix budget: 9KB of whitespace splits into clean trivia that
    # tiles coverage and skips as one significant step.
    ws = b" " * 9216
    ws_pages = chunk(ws, 1024)
    assert probe.run_global(src, "validate_pages", ws_pages, 1024, 9216) == 0
    first = probe.run_global(seg, "next_token_global", ws_pages, 1024, 9216,
                             (integer(0),))
    assert (first["kind"], first["start"], first["diagnostic"]) == (1, 0, 6), \
        first
    assert first["end"] == 8193, first
    batched, batch = batch_walk(probe, ws_pages, 1024, 9216)
    assert all(t["kind"] == 1 for t in batched[:-1]), batched
    assert any(t["diagnostic"] == 6 for t in batched), batched
    check_coverage(batched[:-1], 9216)
    assert batch["eof"] is True
    sig = probe.run_global(seg, "significant_global", ws_pages, 1024, 9216,
                           (integer(0),))
    assert (sig["kind"], sig["start"], sig["diagnostic"]) == (0, 9216, 0), sig
    stats["trivia_prefix"] = {"tokens": len(batched) - 1,
                              "first_prefix_end": first["end"]}
    # Overlong budget: a 9KB identifier is refused, never split.
    ident = b"x" * 9216
    ident_pages = chunk(ident, 1024)
    over = probe.run_global(seg, "next_token_global", ident_pages, 1024, 9216,
                            (integer(0),))
    assert (over["kind"], over["start"], over["end"],
            over["diagnostic"]) == (7, 0, 8193, 5), over
    batched, batch = batch_walk(probe, ident_pages, 1024, 9216)
    assert len(batched) == 1 and batch["eof"] is False, (batched, batch)
    sig = probe.run_global(seg, "significant_global", ident_pages, 1024, 9216,
                           (integer(0),))
    assert (sig["kind"], sig["diagnostic"]) == (7, 5), sig
    stats["overlong"] = {"span": [over["start"], over["end"]]}
    # Header facts over global offsets, including the fuel code.
    headers = [
        (b"mncs 0.18; module a;", 0),
        (b"mncs 0.18; module a.b.c;", 0),
        (b"  // lead\n mncs 0.18; module deep.path.v1; trailing junk ((",
         0),
        (b"mncs 0.18 module a;", 4),
        (b"fn f() -> (r: u64) { return 1; }", 2),
        (b"mncs 0.18; module " + b".".join(b"s%02d" % i for i in range(40))
         + b";", 9),
        (b"mncs 0.18; module " + b".".join(b"s%02d" % i for i in range(20))
         + b";", 0),
    ]
    for text, want_code in headers:
        for stride in [1, 7, 64] if not SMOKE else [7]:
            pages = chunk(text, stride)
            got = probe.run_global(ker, "parse_header_global", pages, stride,
                                   len(text))
            assert got["code"] == want_code, (text[:40], stride, got)
            if want_code == 0:
                assert text[got["version_start"]:got["version_end"]] == b"0.18"
                assert got["module_end"] > got["module_start"]
                stats["headers_ok"] += 1
    # Line/column rendering at boundaries and past the end.
    sample = b"ab\ncde\n\nf" * 30
    for stride in [1, 7, 64] if not SMOKE else [7]:
        pages = chunk(sample, stride)
        for offset in [0, 1, 2, 3, 64, 65, len(sample) - 1, len(sample),
                       len(sample) + 99]:
            rendered = probe.run_global(src, "line_col_global", pages, stride,
                                        len(sample), (integer(offset),))
            assert (rendered["line"], rendered["col"]) == \
                ref_line_col(sample, offset), (stride, offset, rendered)
    stats["line_col"] = True
    # Static resource accounting matches the transported shape.
    cost = probe.run_plain(src, "accounting",
                           [integer(1024), integer(490607)])
    assert cost == {"pages": 480, "bytes": 490607, "capacity": 491520,
                    "byte_at_steps": 1, "validate_steps": 480,
                    "full_scan_steps": 490607}, cost
    assert probe.run_plain(src, "page_count_for",
                           [integer(0), integer(10)]) == 0
    assert probe.run_plain(src, "page_count_for",
                           [integer(16), integer(0)]) == 0
    stats["accounting"] = cost


MILESTONES = [
    ("mncs-compiler/src/compiler/parser.mncs", "parser"),
    ("mncs-compiler/src/compiler/kernel.mncs", "kernel"),
    ("mncs-cli/src/cli/outcome.mncs", "cli-outcome"),
    ("mncs-compiler/src/compiler/lexer.mncs", "lexer"),
    ("mncs-compiler/src/compiler/flow.mncs", "flow"),
    ("mncs-compiler/src/compiler/project.mncs", "project"),
    ("mncs-compiler/src/compiler/ssa.mncs", "ssa"),
    ("mncs-compiler/src/compiler/decl.mncs", "decl"),
]

# Discriminants follow ProjectDiagnostic declaration order.
DIAG_PARSE_FAILED = 4
DIAG_PROOF_FAILED = 5
DIAG_FLOW_FAILED = 6


def discover_sources(root):
    discovered = []
    for path in root.rglob("*.mncs"):
        source_id = path.relative_to(root).as_posix()
        discovered.append((source_id, path, path.read_bytes()))
    return sorted(discovered, key=lambda item: item[0].encode())


def project_request(identities, sources, stride=1024):
    # CP-0021: one flat page array plus per-module page descriptors.
    flat = []
    descriptors = []
    for sid, _, text in sources:
        chunks = chunk(text, stride)
        descriptors.append((sid, len(flat), len(chunks), stride, len(text)))
        flat.extend(chunks)
    project = {
        "record": {
            "type_identity": identities["ProjectSnapshot"]["identity"],
            "name": "ProjectSnapshot",
            "fields": [
                ["fingerprint", byte_sequence(hashlib.sha256(
                    b"".join(len(text).to_bytes(8, "big") + text
                             for _, _, text in sources)).hexdigest().encode())],
                ["sources", {"sequence": {"values": [
                    {"record": {
                        "type_identity":
                            identities["ProjectSource"]["identity"],
                        "name": "ProjectSource",
                        "fields": [
                            ["source_id", byte_sequence(sid.encode())],
                            ["source_path", byte_sequence(sid.encode())],
                            ["page_start", integer(start)],
                            ["page_count", integer(count)],
                            ["stride", integer(st)],
                            ["total", integer(total)]]}}
                    for sid, start, count, st, total in descriptors]}}],
            ],
        }
    }
    return {
        "schema_version": "0.1",
        "target": {"module": "mncs.compiler.project.v1",
                   "function": "compile_project"},
        "arguments": [project, pages_value(flat)],
        "type_arguments": [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)],
        "step_budget": 8_000_000,
    }


def pipeline_run(probe, identities, sources, stride=1024):
    """One native compile_project on the given sources; returns decoded
    native, or None when the backend value arena is exhausted (CP-0023):
    no verdict was reached, so callers must record unknown stages."""
    request = project_request(identities, sources, stride=stride)
    first = probe.send(request)
    probe.count += 1
    probe.steps.append(first.get("steps", 0))
    probe.digest.update(json.dumps(
        ["compile_project", stride, first["status"], first.get("returned"),
         first.get("failure"), first.get("steps")], sort_keys=True).encode())
    if first["status"] == "budget_exhausted":
        return None
    assert first["status"] == "returned", first
    second = probe.send(request)
    assert second["returned"] == first["returned"], \
        "project output must be deterministic"
    return decode(first["returned"][0])


def project_stages(native):
    diags = flist(native["diagnostics"])
    kinds = [d["$v"] for d in diags]
    parse_ok = DIAG_PARSE_FAILED not in kinds and native["module_count"] > 0
    proof_ok = parse_ok and DIAG_PROOF_FAILED not in kinds
    cfg_ok = proof_ok and DIAG_FLOW_FAILED not in kinds
    return {"parse": parse_ok, "proof": proof_ok, "cfg": cfg_ok,
            "verified_ssa": native["value_ssa_valid"] is True}


def first_diag_span(native):
    for diag in flist(native["diagnostics"]):
        payload = diag["$p"]
        if "start" in payload:
            return [payload.get("source_index", 0), payload["start"],
                    payload["end"]]
    return [0, 0, 0]


def git_state(path):
    repo = path
    while not (repo / ".git").exists() and repo != repo.parent:
        repo = repo.parent
    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--short"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    return rev, dirty


def file_strides(total):
    minimum = (total + 1023) // 1024 or 1
    strides = [minimum]
    if (total + 255) // 256 <= PAGE_BOUND and 256 not in strides:
        strides.append(256)
    if 1024 not in strides:
        strides.append(1024)
    return strides


def classify_parse_pressure(data, span, is_ascii):
    """CP-0021: attribute a native parse failure to a known feature gap."""
    if not is_ascii:
        return "CP-0002"
    start = span[1] if isinstance(span, list) and len(span) > 1 else 0
    if data[start:start + 1] == b"<":
        # First generic-fn `<N: Nat>` (or similar) angle bracket: the
        # native declaration parser has no generic-function production.
        return "CP-0015"
    return None


def tier_d(probe, kinds_inv, stats):
    src = "mncs.compiler.source.v1"
    seg = "mncs.compiler.segment.v1"
    ker = "mncs.compiler.kernel.v1"
    workspace = ROOT.parent
    matrix = []
    # One shared project probe for every milestone pipeline plus the
    # anchors: elaboration/retention is paid once, not per milestone.
    project_probe = None
    project_identities = None
    for rel, label in MILESTONES:
        if SMOKE and label not in ("parser", "cli-outcome"):
            continue
        path = workspace / rel
        data = path.read_bytes()
        total = len(data)
        is_ascii = all(byte < 128 for byte in data)
        try:
            text = data.decode("ascii" if is_ascii else "utf-8")
        except UnicodeDecodeError:
            text = None
        rev, dirty = git_state(path.parent)
        row = {"path": rel, "label": label, "revision": rev,
               "tree_dirty": bool(dirty), "bytes": total,
               "representation": None, "strides": {},
               "stages": {"transport": False, "lex": False, "parse": False,
                          "proof": False, "cfg": False, "verified_ssa": False},
               "first_failure": None, "pressure": None, "notes": []}
        raw = oracle_tokens_raw(probe, text) if text is not None else []
        mapped = [(kinds_inv.get(kind, kind), start, end, diag)
                  for kind, start, end, diag in raw]
        first_bad = next((i for i, byte in enumerate(data) if byte >= 128),
                         None)
        if not is_ascii:
            row["non_ascii_first"] = first_bad
            row["notes"].append(
                f"non-ASCII at byte {first_bad} (CP-0002): native admission "
                f"rejects (ascii false, header 8); stride-invariance plus "
                f"attempted oracle compare recorded per stride")
        sig_tokens = [t for t in mapped
                      if not (t[0] in (1, 2, 3) and t[3] == 0)]
        max_sig = max((end - start for _, start, end, _ in sig_tokens),
                      default=0)
        max_trivia = max((end - start for kind, start, end, diag in mapped
                          if kind in (1, 2, 3) and diag == 0), default=0)
        row["oracle_tokens"] = len(mapped)
        row["oracle_significant"] = len(sig_tokens)
        row["oracle_max_significant"] = max_sig
        row["oracle_max_trivia"] = max_trivia
        assert max_sig < 8192, (rel, max_sig)
        budget = 128_000_000 if total > 100_000 else (
            32_000_000 if total > 10_000 else 8_000_000)
        fingerprints = set()
        exact_all = True
        not_gaps = 0
        first_gap = None
        for stride in file_strides(total):
            pages = chunk(data, stride)
            assert len(pages) <= PAGE_BOUND, (rel, stride)
            transport_ok = reassemble(pages) == data
            stats["transport_bytes"] += total
            code = probe.run_global(src, "validate_pages", pages, stride,
                                    total, budget=budget)
            assert code == 0, (rel, stride, code)
            assert probe.run_global(src, "ascii_global", pages, stride, total,
                                    budget=budget) is is_ascii, (rel, stride)
            fp = probe.run_global(src, "fingerprint_global", pages, stride,
                                  total, budget=budget)
            fingerprints.add(fp)
            batched, batch = batch_walk(probe, pages, stride, total,
                                        budget=budget)
            observed = [(t["kind"], t["start"], t["end"], t["diagnostic"])
                        for t in batched]
            assert 5 not in [t["diagnostic"] for t in batched], rel
            oracle_exact = None
            if is_ascii:
                joined = join_trivia_prefixes(observed)
                exact, gaps, gap_span = compare_modulo_not(
                    joined, raw + [("eof", total, total, 0)], data, kinds_inv)
                exact_all = exact_all and exact
                not_gaps = max(not_gaps, gaps)
                if gap_span is not None and first_gap is None:
                    first_gap = gap_span
            else:
                # CP-0002: the native frontend admits ASCII only (header
                # code 8 below). The hard CP-0001 property here is
                # stride-invariance of the native stream; oracle
                # comparison is attempted and recorded, with divergence
                # expected at or past the admission boundary.
                if stride == file_strides(total)[0]:
                    row["stride_reference_tokens"] = len(observed)
                    reference_stream = observed
                else:
                    assert observed == reference_stream, (rel, stride)
                joined = join_trivia_prefixes(observed)
                try:
                    exact, gaps, gap_span = compare_modulo_not(
                        joined, raw + [("eof", total, total, 0)], data,
                        kinds_inv)
                except AssertionError as err:
                    exact, gaps, gap_span = False, 0, None
                    row.setdefault("oracle_divergence", []).append(
                        f"stride {stride}: {err}")
                oracle_exact = exact
                not_gaps = max(not_gaps, gaps)
                exact_all = False
                row["lex_ascii_prefix"] = False
            check_coverage(batched[:-1], total)
            if is_ascii:
                assert trivia_bytes(batched, total) == \
                    oracle_trivia_bytes(mapped, total), (rel, stride)
            assert batch["eof"] is True and batch["next"] == total
            header = probe.run_global(ker, "parse_header_global", pages,
                                      stride, total, budget=budget)
            if is_ascii:
                assert header["code"] == 0, (rel, stride, header)
                assert data[header["version_start"]:header["version_end"]] == \
                    b"0.18"
                row["header_module"] = \
                    data[header["module_start"]:header["module_end"]].decode()
            else:
                assert header["code"] == 8 and header["end"] == total, \
                    (rel, stride, header)
            for offset in [0, total // 2, total]:
                rendered = probe.run_global(src, "line_col_global", pages,
                                            stride, total, (integer(offset),),
                                            budget=budget)
                assert (rendered["line"], rendered["col"]) == \
                    ref_line_col(data, offset), (rel, stride, offset)
            row["strides"][str(stride)] = {
                "pages": len(pages), "transport": transport_ok,
                "fingerprint": fp, "tokens": len(batched) - 1,
                "oracle_exact": oracle_exact,
                "trivia_prefixes": sum(1 for t in observed if t[3] == 6)}
            stats["tokens_compared"] += len(mapped)
        assert len(fingerprints) == 1, (rel, fingerprints)
        row["representation"] = f"logical pages stride varied " \
            f"(min {file_strides(total)[0]}, 1024)"
        row["stages"]["transport"] = True
        row["stages"]["lex"] = exact_all
        if is_ascii:
            row["lex_qualified_modulo_not"] = True
        else:
            row["lex_qualified_modulo_not"] = all(
                info.get("oracle_exact", False)
                for info in row["strides"].values())
        row["lex_gap"] = {"pressure": "CP-0022", "not_tokens": not_gaps,
                          "first_span": first_gap} if not_gaps else None
        elaborated = probe.send({"elaborate": text}) \
            if text is not None else None
        row["oracle_elaborate_count"] = len(elaborated) \
            if elaborated is not None else None
        row["oracle_elaborate_diagnostics"] = elaborated[:5] \
            if elaborated is not None else None
        if total <= 8 * 1024 and text is not None:
            oracle = probe.send({"project_oracle": {"root": text,
                                                   "modules": {}}})
            row["oracle_project_valid"] = oracle["valid"]
            row["oracle_project_diagnostics"] = oracle["diagnostics"][:5]
            row["oracle_project_diagnostic_count"] = \
                len(oracle["diagnostics"])
            row["oracle_has_ssa"] = oracle["ssa"] is not None
        # CP-0021: the real native pipeline runs on the milestone's
        # logical source. Size is representable now, so any failure is a
        # feature gap at a precise span, classified stage by stage.
        if project_probe is None:
            project_probe = full_probe()
            stats["pipeline_execution"] = project_probe.send(
                {"execution_status": True})
            project_identities = {
                record["name"]: record for record in project_probe.send(
                    {"record_types": "mncs.compiler.project.v1"})}
        native = pipeline_run(project_probe, project_identities,
                              [(label, path, data)])
        if native is None:
            # CP-0023: the backend value arena (16 MiB per request)
            # exhausted before any verdict. Lex/transport stages above
            # stand; pipeline stages are unknown, not false.
            row["stages"].update({"parse": None, "proof": None,
                                  "cfg": None, "verified_ssa": None})
            row["backend_arena_exhausted"] = True
            row["first_failure"] = {"stage": "backend-arena", "span": None,
                                    "scope": "cranelift JIT 16MiB value arena"}
            row["pressure"] = "CP-0023"
            row["pressures"] = ["CP-0023"] + \
                (["CP-0022"] if not_gaps else [])
            row["notes"].append("backend arena exhausted; no verdict")
            row["ssa_modules"] = []
            matrix.append(row)
            continue
        stages = project_stages(native)
        row["stages"].update(stages)
        row["native_valid"] = native["valid"]
        row["native_value_ssa_valid"] = native["value_ssa_valid"]
        row["native_module_count"] = native["module_count"]
        if not stages["parse"]:
            span = first_diag_span(native)
            row["first_failure"] = {"stage": "parse", "span": span,
                                    "scope": "native decl.parse_unit"}
            row["pressure"] = classify_parse_pressure(data, span, is_ascii)
            row["pressures"] = ([row["pressure"]] if row["pressure"] else []) \
                + (["CP-0022"] if not_gaps else [])
            if row["pressure"] is None:
                row["notes"].append(
                    f"unclassified native parse gap at span {span}")
        elif not stages["proof"]:
            span = first_diag_span(native)
            row["first_failure"] = {"stage": "proof", "span": span,
                                    "scope": "native decl.prove_unit"}
            row["pressure"] = None
            row["pressures"] = (["CP-0022"] if not_gaps else [])
            row["notes"].append(f"native proof gap at span {span}")
        elif not stages["cfg"]:
            span = first_diag_span(native)
            row["first_failure"] = {"stage": "cfg", "span": span,
                                    "scope": "native flow.lower_unit"}
            row["pressure"] = None
            row["pressures"] = (["CP-0022"] if not_gaps else [])
            row["notes"].append(f"native CFG gap at span {span}")
        elif not stages["verified_ssa"]:
            row["first_failure"] = {"stage": "verified_ssa", "span": None,
                                    "scope": "native ssa.lower_value_ssa"}
            row["pressure"] = None
            row["pressures"] = (["CP-0022"] if not_gaps else [])
            row["notes"].append("native value-SSA gap (see ssa_modules)")
        elif not row["stages"]["lex"]:
            if not is_ascii:
                row["first_failure"] = {"stage": "lex",
                                        "span": [first_bad, first_bad + 1],
                                        "scope": "CP-0002 non-ASCII admission"}
                row["pressure"] = "CP-0002"
                row["pressures"] = ["CP-0002"] + \
                    (["CP-0022"] if not_gaps else [])
            else:
                row["first_failure"] = {"stage": "lex", "span": first_gap,
                                        "scope": "CP-0022 not-pair"}
                row["pressure"] = "CP-0022"
                row["pressures"] = ["CP-0022"]
        else:
            row["first_failure"] = None
            row["pressure"] = None
            row["pressures"] = []
        row["ssa_modules"] = [
            {"source_index": module.get("source_index"),
             "valid": module.get("value_ssa", {}).get("valid"),
             "verified": module.get("value_ssa", {}).get(
                 "verified_function_count"),
             "first_unsupported_kind": module.get("value_ssa", {}).get(
                 "first_unsupported_kind"),
             "first_unsupported_block": module.get("value_ssa", {}).get(
                 "first_unsupported_block")}
            for module in flist(native.get("modules", {"$v": 0}))]
        artifacts = [diag for diag in (elaborated or [])
                     if diag.get("code") == "MNE173" and
                     "unavailable to the resolver" in diag.get("message", "")]
        real = [diag for diag in (elaborated or []) if diag not in artifacts]
        row["oracle_elaborate_artifacts"] = len(artifacts)
        if real:
            row["first_size_free_gap"] = real[0]
            row["notes"].append(f"size-free oracle gap: {real[0]}")
        elif artifacts:
            row["notes"].append(
                f"{len(artifacts)} MNE173 resolver artifact(s): single-file "
                f"elaborate without sibling modules; no non-resolution "
                f"diagnostics, so the oracle parses the file clean")
        matrix.append(row)
    stats["matrix"] = matrix
    try:
        pipeline_anchor(stats, project_probe, project_identities)
    finally:
        if project_probe is not None:
            stats["pipeline_requests"] = project_probe.count
            stats["pipeline_steps_total"] = sum(project_probe.steps)
            stats["pipeline_digest"] = project_probe.digest.hexdigest()
            project_probe.close()


def join_trivia_prefixes(observed):
    """Join diagnostic-6 trivia prefixes with their continuations so the
    joined stream is comparable to the oracle token stream."""
    joined = []
    index = 0
    while index < len(observed):
        kind, start, end, diag = observed[index]
        if diag == 6:
            assert kind in (1, 2, 3), observed[index]
            cursor = end
            index += 1
            while index < len(observed):
                kind2, start2, end2, diag2 = observed[index]
                assert start2 == cursor, (observed[index - 1],
                                         observed[index])
                cursor = end2
                index += 1
                if not (kind2 == kind and diag2 in (0, 6)):
                    joined.append((kind, start, start2, 0))
                    joined.append((kind2, start2, end2, diag2))
                    break
            else:
                joined.append((kind, start, cursor, 0))
        else:
            joined.append((kind, start, end, diag))
            index += 1
    return joined


def pipeline_anchor(stats, probe=None, identities=None):
    """Full native pipeline anchors: the two-module project at the
    1024-byte ceiling (synthetic-1024) plus the same project with its
    root trailing into a third page (synthetic-2049). Both must run the
    whole native pipeline green with oracle agreement; the 2049 row
    additionally proves stride-invariance of the full pipeline by
    comparing stride-1024 and stride-256 runs byte for byte."""
    dependency = ("mncs 0.18; module demo.dep; "
                  "fn answer(value: u64) -> (r: u64) { return value; }")
    root_base = ("mncs 0.18; module demo.root; use demo.dep as dep; "
                 "fn main() -> (r: u64) { return 1; }")
    owned = probe is None
    if owned:
        probe = full_probe()
        probe.send({"execution_status": True})
        identities = {record["name"]: record for record in
                      probe.send({"record_types":
                                  "mncs.compiler.project.v1"})}
    try:
        for label, size, strides in (("synthetic-1024", 1024, (1024,)),
                                     ("synthetic-2049", 2049, (1024, 256))):
            root = root_base + " " * (size - len(root_base.encode()))
            assert len(root.encode()) == size, (label, len(root.encode()))
            row = {"path": f"synthetic:demo.dep+demo.root ({label})",
                   "label": label, "revision": None, "tree_dirty": False,
                   "bytes": size,
                   "representation": "logical pages, all strides agree",
                   "stages": {"transport": True, "lex": True, "parse": False,
                              "proof": False, "cfg": False,
                              "verified_ssa": False},
                   "first_failure": None, "pressure": None, "notes": []}
            with tempfile.TemporaryDirectory(
                    prefix="mncs-cp0001-") as directory:
                project_root = Path(directory)
                (project_root / "b-root.mncs").write_text(root)
                (project_root / "a-dep.mncs").write_text(dependency)
                discovered = discover_sources(project_root)
                natives = {}
                for stride in strides:
                    natives[stride] = pipeline_run(
                        probe, identities, discovered, stride=stride)
                if len(natives) > 1:
                    first_native = next(iter(natives.values()))
                    for stride, native in natives.items():
                        assert native == first_native, \
                            f"pipeline stride-invariance: {label} " \
                            f"stride {stride}"
                    row["stride_invariant"] = True
                native = natives[strides[0]]
                stages = project_stages(native)
                row["stages"].update(stages)
                row["native_valid"] = native["valid"]
                row["native_diagnostics"] = native["diagnostics"]
                assert native["valid"] is True, (label, native["diagnostics"])
                assert all(stages.values()), (label, stages)
                oracle = probe.send({"project_oracle": {
                    "root": root, "modules": {"demo.dep": dependency}}})
                row["oracle_project_valid"] = oracle["valid"]
                row["oracle_project_diagnostics"] = oracle["diagnostics"]
                row["oracle_has_ssa"] = oracle["ssa"] is not None
                assert oracle["valid"] is True, (label, oracle["diagnostics"])
                row["probe_requests"] = probe.count
                row["probe_steps_max"] = max(probe.steps)
            stats["matrix"].append(row)
    finally:
        if owned:
            probe.close()


def fresh_stats():
    return {"transport_bytes": 0, "tokens_compared": 0,
            "fingerprint_shapes": 0, "summary_equivalence": 0, "nonascii": 0,
            "tier_a_cases": 0, "tier_b_cases": 0, "splits_canonical": 0,
            "splits_rejected": 0, "malformed": {}, "transport_faults": 0,
            "trivia_prefix": {}, "overlong": {}, "headers_ok": 0,
            "line_col": False, "accounting": {}, "skipped_page_bound": 0,
            "matrix": []}


def run_suite():
    stats = fresh_stats()
    probe = lex_probe()
    try:
        status = probe.send({"execution_status": True})
        if os.environ.get("MNCS_PROBE_BACKEND", "cranelift") == "cranelift":
            assert status["retained_sessions"] == 3, status
        stats["execution_status"] = status
        kinds = json.loads(Path("src/compiler/token-kinds.json").read_text())
        kinds_inv = {value: int(key) for key, value in kinds.items()}
        if "A" in TIERS:
            tier_a(probe, kinds_inv, stats)
        if "B" in TIERS:
            tier_b(probe, kinds_inv, stats)
        if "C" in TIERS:
            tier_c(probe, kinds_inv, stats)
        if "D" in TIERS:
            tier_d(probe, kinds_inv, stats)
        stats["probe_requests"] = probe.count
        stats["probe_steps_total"] = sum(probe.steps)
        stats["probe_steps_max"] = max(probe.steps) if probe.steps else 0
        stats["result_sha256"] = probe.digest.hexdigest()
    finally:
        probe.close()
    return stats


def print_matrix(matrix):
    print(f"{'label':<16} {'bytes':>7} {'repr':<24} "
          f"{'TlxPPCS':<18} first-failure")
    for row in matrix:
        stages = row["stages"]
        bits = "".join("?" if stages[stage] is None else
                          ("1" if stages[stage] else "0") for stage in
                          ("transport", "lex", "parse", "proof", "cfg",
                           "verified_ssa"))
        failure = row.get("first_failure") or {}
        print(f"{row['label']:<16} {row['bytes']:>7} "
              f"{str(row['representation'])[:24]:<24} {bits:<18} "
              f"{failure.get('stage', '-')}:{failure.get('span', '-')} "
              f"{row.get('pressure') or ''}")


if __name__ == "__main__":
    started = time.monotonic()
    stats = run_suite()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads(
            Path("mncs-language.lock.json").read_text())["revision"],
        "tiers": TIERS,
        "smoke": SMOKE,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "CP-0001 logical immutable source: validation, global "
                 "access, cross-boundary lexing, headers, identity, matrix",
    }
    matrix = stats.pop("matrix")
    report["tests"] = stats
    (OUT / "cp0001-results.json").write_text(json.dumps(report, indent=2)
                                             + "\n")
    (OUT / "cp0001-matrix.json").write_text(json.dumps(matrix, indent=2)
                                            + "\n")
    print_matrix(matrix)
    print(json.dumps({key: stats[key] for key in
                      ("probe_requests", "tokens_compared", "result_sha256",
                       "tier_a_cases", "tier_b_cases", "splits_canonical",
                       "splits_rejected", "headers_ok")}, indent=2))
