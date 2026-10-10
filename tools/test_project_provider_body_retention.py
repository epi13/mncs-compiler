#!/usr/bin/env python3
"""Verify project target parsing selects provider signatures and keeps spans."""

import json
import os

os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.project.v1"
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
    "module": "mncs.compiler.project.v1",
    "function": "compile_project_target",
    "type_arguments": [{"kind": "nat", "value": 1024},
                       {"kind": "nat", "value": 1024}],
}])

import test_project as project


PROJECT_MODULE = "mncs.compiler.project.v1"
NAT_ARGS = [{"kind": "nat", "value": 1024}, {"kind": "nat", "value": 1024}]


def _parse_project_unit(probe, source, source_index, target_source_index, target_only):
    raw = source.encode()
    response = probe.send({
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE, "function": "parse_project_unit"},
        "arguments": [
            project.pages_value([raw]),
            project.integer(1024),
            project.integer(len(raw)),
            project.integer(source_index),
            project.integer(target_source_index),
            {"boolean": {"value": target_only}},
        ],
        "type_arguments": NAT_ARGS,
        "step_budget": 8_000_000,
    })
    assert response.get("status") == "returned", response
    return project.decode(response["returned"][0]), response["steps"]


def _stage0_span(diagnostics, wanted):
    spans = []
    for item in diagnostics:
        span = item.get("span")
        if isinstance(span, dict):
            spans.append((span["start"], span["end"]))
        spans.extend(
            (related["span"]["start"], related["span"]["end"])
            for related in item.get("related", [])
            if isinstance(related.get("span"), dict)
        )
    assert wanted in spans, {"wanted": wanted, "diagnostics": diagnostics}
    return spans


def run():
    provider_bindings = [f"let copy{i}: Value = x;" for i in range(8)]
    provider_statements = provider_bindings + ["return copy7;"]
    provider = (
        "mncs 0.18; module demo.dep; record Value { value: u64 } "
        "fn id(x: Value) -> (r: Value) { "
        + " ".join(provider_statements)
        + " }"
    )
    root = (
        "mncs 0.18; module demo.root; use demo.dep as dep; "
        "fn call(x: dep.Value) -> (r: dep.Value) { return dep.id(x); }"
    )
    probe = project.Probe()
    try:
        provider_unit, provider_steps = _parse_project_unit(
            probe, provider, source_index=0, target_source_index=1, target_only=True)
        target_unit, target_steps = _parse_project_unit(
            probe, provider, source_index=1, target_source_index=1, target_only=True)
        full_project_unit, full_steps = _parse_project_unit(
            probe, provider, source_index=0, target_source_index=1, target_only=False)
        assert provider_unit["ok"] and target_unit["ok"] and full_project_unit["ok"]
        provider_fn = project.flist(provider_unit["fns"], nil=0, cons=1)[0]
        target_fn = project.flist(target_unit["fns"], nil=0, cons=1)[0]
        full_fn = project.flist(full_project_unit["fns"], nil=0, cons=1)[0]
        assert provider_fn["sig"] == target_fn["sig"] == full_fn["sig"]
        assert provider_fn["body"]["has_ret"] is False
        assert project.flist(provider_fn["body"]["body"], nil=0, cons=1) == []
        assert target_fn["body"]["has_ret"] is True
        target_statement_count = len(
            project.flist(target_fn["body"]["body"], nil=0, cons=1)
        )
        assert target_statement_count == len(provider_bindings)
        assert full_fn["body"]["has_ret"] is True
        assert len(project.flist(full_fn["body"]["body"], nil=0, cons=1)) == len(provider_bindings)

        oracle = probe.send({"project_oracle": {
            "root": root,
            "modules": {"demo.dep": provider},
        }})
        assert oracle.get("valid") is True, oracle

        malformed = provider.replace(
            "let copy0: Value = x;",
            "let copy0: Value = ;")
        bad_unit, bad_steps = _parse_project_unit(
            probe, malformed, source_index=0, target_source_index=1, target_only=True)
        assert bad_unit["ok"] is False, bad_unit
        bad_span = (bad_unit["err_start"], bad_unit["err_end"])
        bad_oracle = probe.send({"project_oracle": {
            "root": root,
            "modules": {"demo.dep": malformed},
        }})
        assert bad_oracle.get("valid") is False, bad_oracle
        stage0_spans = _stage0_span(bad_oracle.get("diagnostics", []), bad_span)
        return {
            "schema_version": 1,
            "kind": "project-provider-body-retention",
            "status": "passed",
            "provider_body_discarded_after_full_parse": True,
            "target_body_retained": True,
            "whole_project_parser_body_retained": True,
            "provider_signature_identical": True,
            "provider_body_statement_count": len(provider_bindings),
            "retained_provider_body_statement_count": 0,
            "target_body_statement_count": target_statement_count,
            "stage0_valid_imported_nominal_project": True,
            "malformed_provider_body_rejected": True,
            "provider_parse_error_span": list(bad_span),
            "stage0_diagnostic_spans": [list(span) for span in stage0_spans],
            "stage0_error_span_matches": True,
            "steps": {
                "provider": provider_steps,
                "target": target_steps,
                "whole_project": full_steps,
                "malformed_provider": bad_steps,
            },
        }
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
