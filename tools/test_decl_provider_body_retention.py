#!/usr/bin/env python3
"""Verify target-only provider parsing retains signatures, not body ASTs."""

import json
import os

import test_project as project


DECL_MODULE = "mncs.compiler.decl.v1"
NAT_ARGS = [{"kind": "nat", "value": 1024}, {"kind": "nat", "value": 1024}]


def _probe(modules, execution_modules, seeds, backend):
    os.environ["MNCS_PROBE_MODULES"] = modules
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = execution_modules
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(seeds)
    os.environ["MNCS_PROBE_BACKEND"] = backend
    os.environ.pop("MNCS_PROBE_CACHE_DIR", None)
    return project.Probe()


def _parse_request(function, raw):
    return {
        "schema_version": "0.1",
        "target": {"module": DECL_MODULE, "function": function},
        "arguments": [project.pages_value([raw]), project.integer(1024), project.integer(len(raw))],
        "type_arguments": NAT_ARGS,
        "step_budget": 8_000_000,
    }


def _direct_parser_contract():
    source = (
        "mncs 0.18; module demo.dep; record Value { value: u64 } "
        "fn id(x: Value) -> (r: Value) { let copy: Value = x; return copy; }"
    ).encode()
    seeds = [
        {"module": DECL_MODULE, "function": name, "type_arguments": NAT_ARGS}
        for name in ("parse_unit", "parse_unit_signatures")
    ]
    probe = _probe("source,lexer,parser,segment,decl", DECL_MODULE, seeds,
                   "reference_interpreter")
    try:
        full_response = probe.send(_parse_request("parse_unit", source))
        signature_response = probe.send(_parse_request("parse_unit_signatures", source))
        assert full_response.get("status") == "returned", full_response
        assert signature_response.get("status") == "returned", signature_response
        full = project.decode(full_response["returned"][0])
        signatures = project.decode(signature_response["returned"][0])
        assert full["ok"] and signatures["ok"], (full, signatures)
        assert (full["err_start"], full["err_end"]) == (
            signatures["err_start"], signatures["err_end"])
        full_fns = project.flist(full["fns"], nil=0, cons=1)
        signature_fns = project.flist(signatures["fns"], nil=0, cons=1)
        assert len(full_fns) == len(signature_fns) == 1
        assert full_fns[0]["sig"] == signature_fns[0]["sig"]
        assert full_fns[0]["fn_start"] == signature_fns[0]["fn_start"]
        assert full_fns[0]["fn_end"] == signature_fns[0]["fn_end"]
        assert full_fns[0]["body"]["has_ret"] is True
        assert len(project.flist(full_fns[0]["body"]["body"], nil=0, cons=1)) == 1
        assert signature_fns[0]["body"]["has_ret"] is False
        assert project.flist(signature_fns[0]["body"]["body"], nil=0, cons=1) == []

        malformed_provider = (
            "mncs 0.18; module demo.dep; record Value { value: u64 } "
            "fn id(x: Value) -> (r: Value) { let copy: Value = ; return x; }"
        )
        malformed_bytes = malformed_provider.encode()
        malformed_full_response = probe.send(_parse_request("parse_unit", malformed_bytes))
        malformed_signature_response = probe.send(
            _parse_request("parse_unit_signatures", malformed_bytes))
        assert malformed_full_response.get("status") == "returned", malformed_full_response
        assert malformed_signature_response.get("status") == "returned", malformed_signature_response
        malformed_full = project.decode(malformed_full_response["returned"][0])
        malformed_signatures = project.decode(malformed_signature_response["returned"][0])
        assert malformed_full["ok"] is False and malformed_signatures["ok"] is False
        full_span = (malformed_full["err_start"], malformed_full["err_end"])
        signature_span = (malformed_signatures["err_start"], malformed_signatures["err_end"])
        assert full_span == signature_span, (full_span, signature_span)
        root = (
            "mncs 0.18; module demo.root; use demo.dep as dep; "
            "fn call(x: dep.Value) -> (r: dep.Value) { return x; }"
        )
        oracle = probe.send({"project_oracle": {
            "root": root,
            "modules": {"demo.dep": malformed_provider},
        }})
        assert oracle.get("valid") is False, oracle
        oracle_spans = []
        for item in oracle.get("diagnostics", []):
            span = item.get("span")
            if isinstance(span, dict):
                oracle_spans.append((span["start"], span["end"]))
            oracle_spans.extend(
                (related["span"]["start"], related["span"]["end"])
                for related in item.get("related", [])
                if isinstance(related.get("span"), dict)
            )
        assert full_span in oracle_spans, {
            "native_parse_span": full_span,
            "stage0_diagnostics": oracle.get("diagnostics"),
        }
        return {
            "full_parse_steps": full_response["steps"],
            "signature_parse_steps": signature_response["steps"],
            "same_function_signature_and_span": True,
            "full_body_statement_count": 1,
            "retained_provider_body_statement_count": 0,
            "syntax_parse_completed_before_body_discard": True,
            "malformed_provider_body_rejected": True,
            "full_parser_error_span": list(full_span),
            "signature_parser_error_span": list(signature_span),
            "stage0_project_oracle_error_spans": [list(span) for span in oracle_spans],
            "stage0_error_span_matches": True,
        }
    finally:
        probe.close()


def run():
    parser = _direct_parser_contract()
    return {
        "schema_version": 1,
        "kind": "provider-body-retention-parser-contract",
        "status": "passed",
        "parser_contract": parser,
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
