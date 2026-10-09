#!/usr/bin/env python3
"""Differentially check page-crossing global keyword classification."""

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "tools"))

os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.lexer.v1"
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
    "module": "mncs.compiler.lexer.v1",
    "function": "lex_tokens_from",
    "type_arguments": [
        {"kind": "nat", "value": 64},
        {"kind": "nat", "value": 1024},
    ],
}])

import test_project as project


KEYWORDS = [
    "mncs", "else", "fail", "enum", "next", "over", "true",
    "module", "return", "effect", "record", "fn", "if", "as",
    "let", "use", "requires", "property", "carrying", "ensures",
    "assumes", "iterate", "invariant", "metamorphic", "capability",
    "authorized_by", "match", "up_to", "while", "false",
]

KEYWORD_CODES = {
    "mncs": 10, "module": 11, "fn": 12, "return": 13, "let": 14,
    "if": 15, "else": 16, "fail": 17, "requires": 18, "ensures": 19,
    "assumes": 20, "property": 21, "invariant": 22, "metamorphic": 23,
    "effect": 24, "capability": 25, "authorized_by": 26, "enum": 27,
    "use": 28, "record": 29, "match": 30, "iterate": 31, "up_to": 32,
    "carrying": 33, "next": 34, "over": 35, "as": 36, "while": 37,
    "true": 38, "false": 39,
}


def _cross_page_source():
    parts = []
    cursor = 0
    for word in KEYWORDS:
        start = ((cursor // 1024) + 1) * 1024 - 1
        parts.append(" " * (start - cursor))
        parts.append(word)
        parts.append(" ")
        cursor = start + len(word) + 1
    near_misses = [word + "x" for word in KEYWORDS]
    near_misses += ["x" + word for word in KEYWORDS]
    parts.append(" ".join(near_misses))
    return "".join(parts).encode("ascii")


def run():
    raw = _cross_page_source()
    near_misses = [word + "x" for word in KEYWORDS]
    near_misses += ["x" + word for word in KEYWORDS]
    pages = [raw[index:index + 1024] for index in range(0, len(raw), 1024)]
    assert len(pages) <= 64

    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        request = {
            "schema_version": "0.1",
            "target": {
                "module": "mncs.compiler.lexer.v1",
                "function": "lex_tokens_from",
            },
            "arguments": [
                project.pages_value(pages),
                project.integer(1024),
                project.integer(len(raw)),
                project.integer(0),
            ],
            "type_arguments": [
                {"kind": "nat", "value": 64},
                {"kind": "nat", "value": 1024},
            ],
            "step_budget": 4_000_000,
        }
        response = probe.send(request)
        assert response.get("status") == "returned", response
        result = project.decode(response["returned"][0])
        native_tokens = [
            token for token in project.flist(result["tokens"])
            if token["kind"] != 0
        ]

        oracle = probe.send({"oracle": raw.decode("ascii")})["lexical"]["tokens"]
        kind_names = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
        native = [
            (kind_names[str(token["kind"])], token["start"], token["end"], token["diagnostic"])
            for token in native_tokens
        ]
        expected = [
            (token["kind"], token["span"]["start"], token["span"]["end"], 0)
            for token in oracle
        ]
        assert native == expected, (native[:8], expected[:8])
        assert result["eof"] and result["next"] == len(raw), result

        keyword_tokens = {
            token["kind"]: token for token in native_tokens
            if token["kind"] in KEYWORD_CODES.values()
        }
        assert len(keyword_tokens) == len(KEYWORDS), keyword_tokens
        for word, code in KEYWORD_CODES.items():
            token = keyword_tokens[code]
            assert raw[token["start"]:token["end"]].decode("ascii") == word

        return {
            "status": "passed",
            "backend": execution.get("backend"),
            "keywords": len(KEYWORDS),
            "near_misses": len(near_misses),
            "page_boundary_starts": len(KEYWORDS),
            "source_bytes": len(raw),
            "native_steps": response.get("steps"),
            "token_count": len(native_tokens),
            "stage0_token_count": len(oracle),
            "native_matches_stage0": True,
            "eof_and_global_spans_match": True,
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
