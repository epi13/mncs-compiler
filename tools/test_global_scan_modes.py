#!/usr/bin/env python3
"""Check global scanner modes and page-edge windows against Stage-0."""

import hashlib
import json

import test_global_keyword_dispatch as shared


def fixture():
    raw = bytearray(b" " * 5250)
    for offset, token in (
        (1023, b"alphaBeta123"),
        (2046, b"12.34.5"),
        (3071, b"/* outer /* nested */ tail */"),
        (4060, b"/*" + b"x" * 33 + b"*/"),
        (5118, b"//line crosses page\n"),
    ):
        raw[offset:offset + len(token)] = token
    return bytes(raw)


def run():
    raw = fixture()
    pages = [raw[index:index + 1024] for index in range(0, len(raw), 1024)]
    probe = shared.project.Probe()
    try:
        status = probe.send({"execution_status": True})
        request = {
            "schema_version": "0.1",
            "target": {
                "module": "mncs.compiler.lexer.v1",
                "function": "lex_tokens_from",
            },
            "arguments": [
                shared.project.pages_value(pages),
                shared.project.integer(1024),
                shared.project.integer(len(raw)),
                shared.project.integer(0),
            ],
            "type_arguments": [
                {"kind": "nat", "value": 64},
                {"kind": "nat", "value": 1024},
            ],
            "step_budget": 4_000_000,
        }
        response = probe.send(request)
        assert response.get("status") == "returned", response
        result = shared.project.decode(response["returned"][0])
        native_tokens = [
            token for token in shared.project.flist(result["tokens"])
            if token["kind"] != 0
        ]
        oracle = probe.send({"oracle": raw.decode("ascii")})["lexical"]
        kind_names = json.loads((shared.ROOT / "src/compiler/token-kinds.json").read_text())
        native = [
            (kind_names[str(token["kind"])], token["start"], token["end"], token["diagnostic"])
            for token in native_tokens
        ]
        expected = [
            (token["kind"], token["span"]["start"], token["span"]["end"], 0)
            for token in oracle["tokens"]
        ]
        assert native == expected, (native, expected)
        assert result["eof"] and result["next"] == len(raw), result
        return {
            "status": "passed",
            "backend": status.get("backend"),
            "source_bytes": len(raw),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "page_size": 1024,
            "boundary_cases": [
                "word at offset 1023",
                "number at offset 2046",
                "block opener at offset 3071 with nesting",
                "block closer at offset 4095",
                "line comment at offset 5118 with newline after page edge",
            ],
            "native_steps": response.get("steps"),
            "token_count": len(native_tokens),
            "stage0_token_count": len(oracle["tokens"]),
            "native_matches_stage0": True,
            "eof_and_global_spans_match": True,
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
