"""Verify application-build inventory, selection, and Test batch contracts.

The inventory is derived by mncs-language during the same frontend invocation
that verifies callable roots and emits the VM artifact. This exercises exact
cache reuse, source-location refresh on a comment edit, fresh-process artifact
reuse after an unselected root test body edit, body-sensitive test identity,
and invalid-source suppression without reconstructing tests from source text
in this harness.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import application_build_provider as provider
import test_application_build_provider as provider_tests
import test_vm_native_application as app

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_TEST_INVENTORY_PROBE_BIN",
    ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe",
))
MODULE = "tests.native_inventory_build"
SOURCE_ID = "tests/native_inventory_build.mncs"
SOURCE = """mncs 0.18;
module tests.native_inventory_build;

fn selected_entry() -> (result: u64) { return 7; }
test second_case() -> (result: u64) { return 2; }
test first_case() -> (result: u64) { return 1; }
"""


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request(
    source: str,
    callable_roots: list[dict] | None = None,
    *,
    root_source_id: str = SOURCE_ID,
    modules: dict[str, str] | None = None,
) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/3",
        "target": "mncs-vm-0.1",
        "root_source_id": root_source_id,
        "root_source": source,
        "callable_roots": ([{"module": MODULE, "function": "selected_entry"}]
                            if callable_roots is None else callable_roots),
        "modules": {} if modules is None else modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "application-build-source-test-inventory/1",
    }


def require_inventory(
    response: dict,
    expected_names: set[str] | None = None,
    expected_module: str = MODULE,
    response_schema: str = "mncs.compiler.application-build/6",
) -> dict:
    if (response.get("schema_version") != response_schema
            or response.get("status") != "verified"):
        raise AssertionError(f"{response_schema} did not verify: " + json.dumps({
            "schema_version": response.get("schema_version"),
            "status": response.get("status"),
            "diagnostics": response.get("diagnostics"),
        }, indent=2))
    inventory = response.get("source_test_inventory")
    if not isinstance(inventory, dict):
        raise TypeError("verified build omitted its compiler-issued source Test inventory")
    if (inventory.get("schema_version") != "mncs.test-inventory/1"
            or inventory.get("scope") != "source_module"
            or inventory.get("module") != expected_module):
        raise AssertionError("application-build inventory has the wrong Language contract")
    tests = inventory.get("tests")
    expected_names = {"first_case", "second_case"} if expected_names is None else expected_names
    if not isinstance(tests, list) or {row.get("name") for row in tests} != expected_names:
        raise AssertionError("source Test inventory did not retain exactly the root test declarations")
    if any(not row.get("test_case_identity") or not row.get("function_identity")
           or not row.get("declaration_identity") or not row.get("semantic_fingerprint")
           for row in tests):
        raise AssertionError("source Test inventory lost compiler-issued declaration identities")
    if not inventory.get("source_artifact_identity") or not inventory.get("subject_fingerprint"):
        raise AssertionError("source Test inventory omitted its source or subject identity")
    return inventory


def frontend_invocations(response: dict) -> int | None:
    return (response.get("cache", {}).get("verification_work", {})
            .get("front_end_invocations"))


def require_language_fact_v3(response: dict, label: str) -> dict:
    work = response.get("cache", {}).get("verification_work", {})
    if work.get("language_verification_fact_schema") != "mncs.language-verification-fact/3":
        raise AssertionError(f"{label} did not consume Language fact v3: {work}")
    if (not isinstance(work.get("reused_semantic_facts"), list)
            or not isinstance(work.get("recomputed_semantic_facts"), list)
            or type(work.get("produced_semantic_fact_count")) is not int):
        raise AssertionError(f"{label} omitted actual Language fact work: {work}")
    return work


def verify_build_assumption_cache_isolation(directory: Path) -> dict:
    """Exercise capability and effect assumptions through real application builds."""
    program_cache = directory / "program-cache"
    application_cache = directory / "application-cache"
    baseline_request = request(SOURCE)
    capability_request = request(SOURCE)
    capability_request["capabilities"] = ["test_artifact.read"]
    effect_request = request(SOURCE)
    effect_request["effect_assumptions"] = [{
        "capability": "test_artifact.read",
        "policy": "bounded-artifact/1",
    }]

    probe = app.Probe(
        program_cache,
        executable=PROBE,
        application_cache_dir=application_cache,
    )
    try:
        baseline, baseline_metrics = probe.send(
            {"compile_application": baseline_request}, timeout=180.0
        )
        capability, capability_metrics = probe.send(
            {"compile_application": capability_request}, timeout=180.0
        )
        effect, effect_metrics = probe.send(
            {"compile_application": effect_request}, timeout=180.0
        )
    finally:
        probe.close()

    exact_probe = app.Probe(
        program_cache,
        executable=PROBE,
        application_cache_dir=application_cache,
    )
    try:
        exact_effect, exact_metrics = exact_probe.send(
            {"compile_application": effect_request}, timeout=90.0
        )
    finally:
        exact_probe.close()

    responses = {
        "baseline": baseline,
        "changed_capabilities": capability,
        "changed_effect_assumptions": effect,
    }
    for label, response in responses.items():
        if (response.get("schema_version") != "mncs.compiler.application-build/6"
                or response.get("status") != "verified"
                or response.get("cache", {}).get("status") != "miss"
                or frontend_invocations(response) != 1
                or not isinstance(response.get("artifact", {}).get("value"), dict)):
            raise AssertionError(
                f"{label} policy build did not independently verify: "
                + json.dumps({
                    "status": response.get("status"),
                    "cache": response.get("cache"),
                    "diagnostics": response.get("diagnostics"),
                }, indent=2)
            )

    keys = {label: response["cache"]["key"] for label, response in responses.items()}
    if len(set(keys.values())) != len(keys):
        raise AssertionError("capability or effect changes shared an application-build cache key")
    if capability.get("build_assumptions", {}).get("capabilities") != ["test_artifact.read"]:
        raise AssertionError("capability variant lost its current build assumption")
    if effect.get("build_assumptions", {}).get("effect_assumptions") != effect_request[
        "effect_assumptions"
    ]:
        raise AssertionError("effect variant lost its current build assumption")
    artifact_identities = {
        label: response.get("artifact", {}).get("identity")
        for label, response in responses.items()
    }
    if len(set(artifact_identities.values())) != 1:
        raise AssertionError("policy metadata changed the executable artifact for identical source")
    if (exact_effect.get("cache", {}).get("status") != "hit"
            or frontend_invocations(exact_effect) != 0
            or exact_effect.get("cache", {}).get("key") != keys["changed_effect_assumptions"]
            or exact_effect.get("build_assumptions", {}).get("effect_assumptions")
            != effect_request["effect_assumptions"]
            or exact_effect.get("artifact", {}).get("identity")
            != artifact_identities["changed_effect_assumptions"]):
        raise AssertionError("fresh-process exact policy reuse returned an incompatible build")

    return {
        "request_schema": baseline_request["schema_version"],
        "cache_observation_schema": baseline["cache"].get("schema_version"),
        "source_snapshot_identity": baseline.get("source_snapshot", {}).get("identity"),
        "application_cache_keys": keys,
        "artifact_identities": artifact_identities,
        "fresh_process_exact_hit": {
            "status": exact_effect.get("cache", {}).get("status"),
            "front_end_invocations": frontend_invocations(exact_effect),
            "key": exact_effect.get("cache", {}).get("key"),
        },
        "language_verified_facts_reused": 0,
        "measurements": {
            "baseline": baseline_metrics,
            "changed_capabilities": capability_metrics,
            "changed_effect_assumptions": effect_metrics,
            "fresh_process_exact_hit": exact_metrics,
        },
        "verification_work": {
            "baseline": baseline.get("cache", {}).get("verification_work"),
            "changed_capabilities": capability.get("cache", {}).get("verification_work"),
            "changed_effect_assumptions": effect.get("cache", {}).get("verification_work"),
            "fresh_process_exact_hit": exact_effect.get("cache", {}).get("verification_work"),
        },
    }


def inventory_test_arguments(
    row: dict,
    arguments_by_test_case: dict[str, list[dict]] | None,
    type_arguments: list[dict],
) -> list[dict]:
    """Validate typed values against the Language-issued Test signature.

    The current native Test batch slice admits u64, i64, bool, and byte inputs
    plus bounded [byte; N] inputs whose lengths follow the Language-issued
    generic parameter order. Raw caller JSON is never passed through unchecked.
    """
    identity = row.get("test_case_identity")
    if not isinstance(identity, str) or not identity:
        raise AssertionError("Language Test inventory omitted its test-case identity")
    generic_params = row.get("generic_params")
    if generic_params is None:
        generic_params = []
    if not isinstance(generic_params, list):
        raise TypeError(f"Language Test inventory has malformed generic parameters: {row}")
    inputs = row.get("inputs")
    if not isinstance(inputs, list):
        raise TypeError(f"Language Test inventory has malformed inputs: {row}")
    supplied = (arguments_by_test_case or {}).get(identity, [])
    if not isinstance(supplied, list):
        raise TypeError(f"typed Test arguments must be a list for {identity}")
    if len(inputs) != len(supplied):
        raise AssertionError(
            f"typed Test argument count differs from the Language signature for {identity}"
        )

    bounds: dict[str, int] = {}
    if generic_params:
        if len(generic_params) != len(type_arguments):
            raise AssertionError(
                f"generic Test type arguments differ from its Language signature: {row}"
            )
        for parameter, argument in zip(generic_params, type_arguments):
            if (not isinstance(parameter, dict)
                    or parameter.get("kind") != "nat"
                    or not isinstance(argument, dict)
                    or argument.get("kind") != "nat"):
                raise AssertionError(
                    f"only Nat generic Test parameters are currently admitted: {row}"
                )
            name = parameter.get("name")
            bound = argument.get("value")
            if (not isinstance(name, str) or not name
                    or name in bounds
                    or type(bound) is not int
                    or not 1 <= bound <= 64):
                raise AssertionError(
                    f"generic Test parameters require unique names and bounds in 1..64: {row}"
                )
            bounds[name] = bound
    elif type_arguments:
        raise AssertionError(f"non-generic Test {identity} received generic arguments")

    for input_row, value in zip(inputs, supplied):
        if not isinstance(input_row, dict) or not isinstance(input_row.get("type"), str):
            raise TypeError(f"Language Test inventory has a malformed input: {input_row}")
        input_type = input_row["type"]
        if input_type == "u64":
            if (not isinstance(value, dict)
                    or set(value) != {"integer"}
                    or not isinstance(value["integer"], dict)
                    or set(value["integer"]) != {"type", "value"}
                    or value["integer"].get("type") != {"bits": 64, "signed": False}
                    or type(value["integer"].get("value")) is not int
                    or not 0 <= value["integer"]["value"] <= (1 << 64) - 1):
                raise AssertionError(
                    f"Test input for {identity} is not a valid typed u64 wire value"
                )
            continue

        if input_type == "i64":
            if (not isinstance(value, dict)
                    or set(value) != {"integer"}
                    or not isinstance(value["integer"], dict)
                    or set(value["integer"]) != {"type", "value"}
                    or value["integer"].get("type") != {"bits": 64, "signed": True}
                    or type(value["integer"].get("value")) is not int
                    or not -(1 << 63) <= value["integer"]["value"] <= (1 << 63) - 1):
                raise AssertionError(
                    f"Test input for {identity} is not a valid typed i64 wire value"
                )
            continue

        if input_type == "bool":
            if (not isinstance(value, dict)
                    or set(value) != {"boolean"}
                    or not isinstance(value["boolean"], dict)
                    or set(value["boolean"]) != {"value"}
                    or type(value["boolean"].get("value")) is not bool):
                raise AssertionError(
                    f"Test input for {identity} is not a valid typed bool wire value"
                )
            continue

        if input_type == "byte":
            if (not isinstance(value, dict)
                    or set(value) != {"byte"}
                    or not isinstance(value["byte"], dict)
                    or set(value["byte"]) != {"value"}
                    or type(value["byte"].get("value")) is not int
                    or not 0 <= value["byte"]["value"] <= 255):
                raise AssertionError(
                    f"Test input for {identity} is not a valid typed byte wire value"
                )
            continue

        sequence_match = re.fullmatch(
            r"\[byte;\s*([A-Za-z_][A-Za-z0-9_]*)\]", input_type
        )
        if sequence_match and sequence_match.group(1) in bounds:
            bound = bounds[sequence_match.group(1)]
            byte_values = None
            sequence = value.get("sequence") if isinstance(value, dict) else None
            if isinstance(sequence, dict):
                byte_values = sequence.get("values")
            if (not isinstance(value, dict)
                    or set(value) != {"sequence"}
                    or not isinstance(value["sequence"], dict)
                    or set(value["sequence"]) != {"values"}
                    or not isinstance(byte_values, list)
                    or len(byte_values) != bound
                    or any(not isinstance(item, dict)
                           or set(item) != {"byte"}
                           or not isinstance(item["byte"], dict)
                           or set(item["byte"]) != {"value"}
                           or type(item["byte"].get("value")) is not int
                           or not 0 <= item["byte"]["value"] <= 255
                           for item in byte_values)):
                raise AssertionError(
                    f"Test input for {identity} is not a bounded byte sequence of length {bound}"
                )
            continue
        raise AssertionError(
            f"Test input for {identity} has an unsupported Language type: {input_type}"
        )
    return supplied

def inventory_test_type_arguments(
    row: dict, type_arguments_by_test_case: dict[str, list[dict]] | None
) -> list[dict]:
    identity = row.get("test_case_identity")
    if not isinstance(identity, str) or not identity:
        raise AssertionError("Language Test inventory omitted its test-case identity")
    generic_params = row.get("generic_params")
    if generic_params is None:
        generic_params = []
    if not isinstance(generic_params, list):
        raise TypeError(f"Language Test inventory has malformed generic parameters: {row}")
    supplied = (type_arguments_by_test_case or {}).get(identity, [])
    if not isinstance(supplied, list):
        raise TypeError(f"Test type arguments must be a list for {identity}")
    if not generic_params:
        if supplied:
            raise AssertionError(f"non-generic Test {identity} received type arguments")
        return []
    if len(generic_params) != len(supplied):
        raise AssertionError(
            f"generic Test type arguments differ from its Language signature: {row}"
        )
    names = set()
    for parameter, argument in zip(generic_params, supplied):
        if (not isinstance(parameter, dict)
                or parameter.get("kind") != "nat"
                or not isinstance(parameter.get("name"), str)
                or not parameter["name"]
                or parameter["name"] in names
                or not isinstance(argument, dict)
                or set(argument) != {"kind", "value"}
                or argument.get("kind") != "nat"
                or type(argument.get("value")) is not int
                or not 1 <= argument["value"] <= 64):
            raise AssertionError(
                f"generic Test parameters require unique Nat names and bounds in 1..64: {row}"
            )
        names.add(parameter["name"])
    return supplied


def require_inventory_rejection(description: str, operation) -> None:
    try:
        operation()
    except AssertionError:
        return
    raise AssertionError(f"inventory accepted invalid {description}")


def execute_inventory_batch(
    artifact: dict,
    inventory: dict,
    step_budget: int = 100_000,
    repeat_count: int = 1,
    arguments_by_test_case: dict[str, list[dict]] | None = None,
    type_arguments_by_test_case: dict[str, list[dict]] | None = None,
    selected_cases: list[dict] | None = None,
) -> tuple[dict, float, int | None]:
    tests = inventory.get("tests", [])
    inventory_module = inventory.get("module")
    if not isinstance(inventory_module, str) or not inventory_module:
        raise AssertionError("source Test inventory omitted its module identity")
    if not isinstance(tests, list):
        raise AssertionError("source Test inventory omitted its case list")
    if type(repeat_count) is not int or not 1 <= repeat_count <= 8:
        raise ValueError("inventory batch repeat count must be between 1 and 8")
    inventory_by_identity: dict[str, dict] = {}
    for row in tests:
        identity = row.get("test_case_identity") if isinstance(row, dict) else None
        if (not isinstance(identity, str) or not identity
                or identity in inventory_by_identity):
            raise AssertionError("source Test inventory contains a missing or duplicate case identity")
        if row.get("module") != inventory_module:
            raise AssertionError(f"inventory test is outside its source module: {row}")
        inventory_by_identity[identity] = row

    if selected_cases is None:
        cases = tests
    else:
        if not isinstance(selected_cases, list) or not selected_cases:
            raise ValueError("selected Test cases must be a nonempty Language selection")
        cases = []
        selected_identities: set[str] = set()
        for selected in selected_cases:
            identity = selected.get("test_case_identity") if isinstance(selected, dict) else None
            if not isinstance(identity, str) or not identity:
                raise AssertionError("selected Test case omitted its Language identity")
            if identity in selected_identities:
                raise AssertionError(f"selected Test case identity is duplicated: {identity}")
            current = inventory_by_identity.get(identity)
            if current is None or current != selected:
                raise AssertionError(
                    "selected Test case does not exactly match the current Language inventory: "
                    f"{identity}"
                )
            selected_identities.add(identity)
            cases.append(current)

    test_identities = {row["test_case_identity"] for row in cases}
    unknown_arguments = set(arguments_by_test_case or {}) - test_identities
    if unknown_arguments:
        raise AssertionError(
            "typed Test arguments name identities outside the Language inventory: "
            f"{sorted(unknown_arguments)}"
        )
    unknown_type_arguments = set(type_arguments_by_test_case or {}) - test_identities
    if unknown_type_arguments:
        raise AssertionError(
            "Test type arguments name identities outside the Language inventory: "
            f"{sorted(unknown_type_arguments)}"
        )
    calls = []
    for repeat_index in range(repeat_count):
        for row in cases:
            call_id = row["test_case_identity"]
            if repeat_count > 1:
                call_id = f"{call_id}:round:{repeat_index + 1}"
            type_arguments = inventory_test_type_arguments(row, type_arguments_by_test_case)
            calls.append({
                "id": call_id,
                "request": {
                    "schema_version": "0.1",
                    "target": {"module": row["module"], "function": row["name"]},
                    "arguments": inventory_test_arguments(
                        row, arguments_by_test_case, type_arguments
                    ),
                    "type_arguments": type_arguments,
                    "step_budget": step_budget,
                },
            })
    with tempfile.TemporaryDirectory(prefix="mncs-test-inventory-batch-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n", encoding="utf-8")
        started = time.monotonic()
        completed = subprocess.run(
            ["/usr/bin/time", "-v", str(app.VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(f"canonical VM inventory batch failed: {completed.stderr[-2500:]}")
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        peak_rss = int(match.group(1)) if match else None
        return json.loads(output_path.read_text(encoding="utf-8")), wall, peak_rss


def run_parameterized_test_case() -> dict:
    """Run one typed source Test through the compiled artifact and Stage-0 oracle."""
    module = "tests.parameterized_inventory"
    test_name = "assert_input"
    source = """mncs 0.18;
module tests.parameterized_inventory;
use mncs.test.assertions;

fn selected_entry() -> (result: u64) { return 0; }
test assert_input(expected: u64, actual: u64, signed: i64, enabled: bool, marker: byte) -> (result: TestResult) {
    let unsigned_pair: Assertion = equals_i64(expected as i64, actual as i64, 2001);
    let signed_pair: Assertion = equals_i64(signed, -17, 2002);
    let bool_pair: Assertion = equals_bool(true, enabled, 2003);
    let byte_pair: Assertion = equals_i64(marker as i64, 255, 2005);
    return from_assertion(equals_bool(
        true,
        unsigned_pair.passed && signed_pair.passed && bool_pair.passed && byte_pair.passed,
        2006
    ));
}
"""
    assertions_path = app.TEST_ROOT / "native/mncs/test/assertions.mncs"
    assertions_source = assertions_path.read_text(encoding="utf-8")
    modules = {"mncs.test.assertions": assertions_source}
    build_request = {
        "schema_version": "mncs.compiler.application-build-request/4",
        "target": "mncs-vm-0.1",
        "root_source_id": "tests/parameterized_inventory.mncs",
        "root_source": source,
        "callable_roots": [{"module": module, "function": "selected_entry"}],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "application-build-parameterized-test/1",
        "test_selection": {"kind": "all_source_cases"},
    }
    value = 23
    typed_value = {
        "integer": {"type": {"bits": 64, "signed": False}, "value": value}
    }
    signed_value = -17
    typed_signed_value = {
        "integer": {"type": {"bits": 64, "signed": True}, "value": signed_value}
    }
    typed_bool_value = {"boolean": {"value": True}}
    typed_byte_value = {"byte": {"value": 255}}
    typed_arguments = [
        typed_value, typed_value, typed_signed_value, typed_bool_value, typed_byte_value
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-test-parameterized-case-") as raw:
        directory = Path(raw)
        probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send(
                {"compile_application": build_request}, timeout=180.0
            )
        finally:
            probe.close()
        inventory = require_inventory(
            built,
            {test_name},
            expected_module=module,
            response_schema="mncs.compiler.application-build/7",
        )
        test_row = inventory["tests"][0]
        if (test_row.get("inputs") != [
                {"name": "expected", "type": "u64"},
                {"name": "actual", "type": "u64"},
                {"name": "signed", "type": "i64"},
                {"name": "enabled", "type": "bool"},
                {"name": "marker", "type": "byte"},
        ]
                or test_row.get("generic_params")
                or frontend_invocations(built) != 1):
            raise AssertionError("Language inventory did not retain the typed Test signature")
        test_identity = test_row["test_case_identity"]
        require_inventory_rejection(
            "multi-input argument count",
            lambda: inventory_test_arguments(
                test_row, {test_identity: [typed_value]}, []
            ),
        )
        wrong_wire_type = {
            "integer": {"type": {"bits": 32, "signed": False}, "value": value}
        }
        require_inventory_rejection(
            "u64 wire type",
            lambda: inventory_test_arguments(
                test_row,
                {
                    test_identity: [
                        typed_value, wrong_wire_type, typed_signed_value,
                        typed_bool_value, typed_byte_value
                    ]
                },
                [],
            ),
        )
        wrong_signed_type = {
            "integer": {"type": {"bits": 64, "signed": False}, "value": signed_value}
        }
        require_inventory_rejection(
            "i64 signed wire type",
            lambda: inventory_test_arguments(
                test_row,
                {
                    test_identity: [
                        typed_value, typed_value, wrong_signed_type,
                        typed_bool_value, typed_byte_value
                    ]
                },
                [],
            ),
        )
        out_of_range_signed_value = {
            "integer": {"type": {"bits": 64, "signed": True}, "value": 1 << 63}
        }
        require_inventory_rejection(
            "i64 range",
            lambda: inventory_test_arguments(
                test_row,
                {
                    test_identity: [
                        typed_value, typed_value, out_of_range_signed_value,
                        typed_bool_value, typed_byte_value
                    ]
                },
                [],
            ),
        )
        malformed_bool_value = {"boolean": {"value": 1}}
        require_inventory_rejection(
            "bool wire value type",
            lambda: inventory_test_arguments(
                test_row,
                {
                    test_identity: [
                        typed_value, typed_value, typed_signed_value,
                        malformed_bool_value, typed_byte_value
                    ]
                },
                [],
            ),
        )
        out_of_range_byte_value = {"byte": {"value": 256}}
        require_inventory_rejection(
            "byte range",
            lambda: inventory_test_arguments(
                test_row,
                {
                    test_identity: [
                        typed_value, typed_value, typed_signed_value,
                        typed_bool_value, out_of_range_byte_value
                    ]
                },
                [],
            ),
        )
        artifact = built.get("artifact", {}).get("value")
        if not isinstance(artifact, dict):
            raise AssertionError("parameterized Test build omitted its verified VM artifact")
        artifact_callables = {
            (row.get("module"), row.get("name"))
            for row in artifact.get("callables", [])
        }
        if (module, test_name) not in artifact_callables:
            raise AssertionError("verified application omitted the selected parameterized Test")

        repeat_count = 2
        arguments_by_test_case = {
            test_row["test_case_identity"]: typed_arguments,
        }
        calls = [{
            "schema_version": "0.1",
            "target": {"module": module, "function": test_name},
            "arguments": typed_arguments,
            "type_arguments": [],
            "step_budget": 100_000,
        } for _ in range(repeat_count)]
        oracle_probe = app.Probe(directory / "oracle-cache", executable=PROBE)
        try:
            oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": source,
                "modules": modules,
                "calls": calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        if (not oracle.get("valid")
                or len(oracle.get("executions", [])) != repeat_count):
            raise AssertionError("Stage-0 did not execute both typed Test input calls")

        vm, vm_wall, vm_peak_rss = execute_inventory_batch(
            artifact,
            inventory,
            repeat_count=repeat_count,
            arguments_by_test_case=arguments_by_test_case,
        )
        rows = vm.get("results", [])
        if (vm.get("artifact_id") != artifact.get("artifact_id")
                or len(rows) != repeat_count
                or built.get("artifact", {}).get("identity") != artifact.get("artifact_id")):
            raise AssertionError("typed Test batch did not retain its verified artifact identity")
        actual_result = None
        for repeat_index, (actual_row, expected_row) in enumerate(zip(
            rows, oracle["executions"]
        )):
            expected_id = test_row["test_case_identity"]
            if repeat_count > 1:
                expected_id = f"{expected_id}:round:{repeat_index + 1}"
            if (actual_row.get("id") != expected_id
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError("typed Test invocation did not complete under its inventory identity")
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError("typed Test returned the wrong result arity")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError("canonical VM typed Test result differs from Stage-0")
            if (not isinstance(actual, dict)
                    or actual.get("verdict_code") != 0
                    or actual.get("assertions") != 1
                    or actual.get("failures") != 0
                    or actual.get("expected") != 1
                    or actual.get("actual") != 1):
                raise AssertionError(
                    f"typed Test did not return the expected passing TestResult: {actual}"
                )
            if actual_result is None:
                actual_result = actual
            elif actual_result != actual:
                raise AssertionError("repeated typed Test invocation changed its result")

        return {
            "test_repository_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=app.TEST_ROOT, text=True
            ).strip(),
            "assertions_source": {
                "path": "native/mncs/test/assertions.mncs",
                "sha256": sha(assertions_source.encode("utf-8")),
            },
            "test_case_identity": test_row["test_case_identity"],
            "input_signature": test_row["inputs"],
            "input_values": {
                "expected": value,
                "actual": value,
                "signed": signed_value,
                "enabled": True,
                "marker": 255,
            },
            "verified_artifact_identity": built["artifact"]["identity"],
            "canonical_vm_artifact_identity": vm["artifact_id"],
            "canonical_vm_batch_count": len(rows),
            "repeat_results_match": True,
            "stage0_equivalent": True,
            "invalid_argument_count_rejected": True,
            "invalid_u64_wire_type_rejected": True,
            "invalid_i64_wire_type_rejected": True,
            "out_of_range_i64_rejected": True,
            "invalid_bool_wire_value_rejected": True,
            "out_of_range_byte_rejected": True,
            "typed_test_result": actual_result,
            "build_frontend_invocations": frontend_invocations(built),
            "build_process_metrics": build_metrics,
            "stage0_oracle_metrics": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss,
        }


def run_generic_test_case() -> dict:
    """Run a Language-inventoried generic Test through its seeded VM entrypoint."""
    module = "tests.generic_inventory"
    test_name = "bytes"
    first_bound = 2
    second_bound = 3
    source = """mncs 0.18;
module tests.generic_inventory;
use mncs.test.assertions;

fn selected_entry() -> (result: u64) { return 0; }
test bytes<N: Nat, M: Nat>(expected: [byte; N], actual: [byte; M]) -> (result: TestResult) {
    let first: Assertion = equals_bytes<N>(expected, expected, 2002);
    let second: Assertion = equals_bytes<M>(actual, actual, 2003);
    return from_assertion(equals_bool(true, first.passed && second.passed, 2004));
}
"""
    assertions_path = app.TEST_ROOT / "native/mncs/test/assertions.mncs"
    assertions_source = assertions_path.read_text(encoding="utf-8")
    modules = {"mncs.test.assertions": assertions_source}
    type_arguments = [
        {"kind": "nat", "value": first_bound},
        {"kind": "nat", "value": second_bound},
    ]
    generic_seed = {
        "module": module,
        "function": test_name,
        "type_arguments": type_arguments,
    }
    build_request = {
        "schema_version": "mncs.compiler.application-build-request/4",
        "target": "mncs-vm-0.1",
        "root_source_id": "tests/generic_inventory.mncs",
        "root_source": source,
        "callable_roots": [{"module": module, "function": "selected_entry"}],
        "modules": modules,
        "generic_seeds": [generic_seed],
        "capabilities": [],
        "verification_policy": "application-build-generic-test/1",
        "test_selection": {"kind": "all_source_cases"},
    }
    first_byte_values = [0, 1]
    second_byte_values = [2, 3, 4]
    typed_arguments = [
        {"sequence": {"values": [{"byte": {"value": item}} for item in first_byte_values]}},
        {"sequence": {"values": [{"byte": {"value": item}} for item in second_byte_values]}},
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-test-generic-case-") as raw:
        directory = Path(raw)
        probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send(
                {"compile_application": build_request}, timeout=180.0
            )
        finally:
            probe.close()
        inventory = require_inventory(
            built,
            {test_name},
            expected_module=module,
            response_schema="mncs.compiler.application-build/7",
        )
        test_row = inventory["tests"][0]
        if (test_row.get("generic_params") != [
                    {"kind": "nat", "name": "N"},
                    {"kind": "nat", "name": "M"},
                ]
                or test_row.get("inputs") != [
                    {"name": "expected", "type": "[byte; N]"},
                    {"name": "actual", "type": "[byte; M]"},
                ]
                or frontend_invocations(built) != 1):
            raise AssertionError("Language inventory did not retain the generic Test signature")
        test_identity = test_row["test_case_identity"]
        require_inventory_rejection(
            "generic specialization arity",
            lambda: inventory_test_type_arguments(
                test_row, {test_identity: type_arguments[:1]}
            ),
        )
        malformed_sequence_arguments = [
            typed_arguments[0],
            {"sequence": []},
        ]
        require_inventory_rejection(
            "generic byte input shape",
            lambda: inventory_test_arguments(
                test_row,
                {test_identity: malformed_sequence_arguments},
                type_arguments,
            ),
        )
        artifact = built.get("artifact", {}).get("value")
        if not isinstance(artifact, dict):
            raise AssertionError(
                "generic Test build did not emit a verified specialization: "
                + json.dumps({
                    "status": built.get("status"),
                    "unresolved_obligations": built.get("unresolved_obligations"),
                }, sort_keys=True)
            )
        generic_entrypoints = artifact.get("generic_entrypoints", [])
        matching_entrypoints = [row for row in generic_entrypoints
                                if row.get("generic_module") == module
                                and row.get("generic_function") == test_name
                                and row.get("args_spellings") == [str(first_bound), str(second_bound)]]
        if len(matching_entrypoints) != 1:
            raise AssertionError(
                "generic Test artifact omitted its exact Nat specialization entrypoint"
            )
        entrypoint = matching_entrypoints[0]
        if not any(row.get("function") == entrypoint.get("function")
                   for row in artifact.get("callables", [])):
            raise AssertionError("generic Test entrypoint does not name an emitted callable")
        selected_roots = built.get("callable_scope", {}).get("callable_roots", [])
        selected_test = next((row for row in selected_roots
                              if row.get("module") == module
                              and row.get("function") == test_name), None)
        if (built.get("status") != "verified"
                or built.get("unresolved_obligations") != []
                or selected_test is None
                or selected_test.get("status") != "verified"):
            raise AssertionError("Compiler did not bind the selected generic Test to its seed")

        repeat_count = 2
        test_identity = test_row["test_case_identity"]
        arguments_by_test_case = {test_identity: typed_arguments}
        type_arguments_by_test_case = {test_identity: type_arguments}
        calls = [{
            "schema_version": "0.1",
            "target": {"module": module, "function": test_name},
            "arguments": typed_arguments,
            "type_arguments": type_arguments,
            "step_budget": 100_000,
        } for _ in range(repeat_count)]
        oracle_probe = app.Probe(directory / "oracle-cache", executable=PROBE)
        try:
            oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": source,
                "modules": modules,
                "generic_seeds": [generic_seed],
                "calls": calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        if (not oracle.get("valid")
                or len(oracle.get("executions", [])) != repeat_count):
            raise AssertionError("Stage-0 did not execute both generic Test calls")

        vm, vm_wall, vm_peak_rss = execute_inventory_batch(
            artifact,
            inventory,
            repeat_count=repeat_count,
            arguments_by_test_case=arguments_by_test_case,
            type_arguments_by_test_case=type_arguments_by_test_case,
        )
        rows = vm.get("results", [])
        if (vm.get("artifact_id") != artifact.get("artifact_id")
                or len(rows) != repeat_count
                or built.get("artifact", {}).get("identity") != artifact.get("artifact_id")):
            raise AssertionError("generic Test batch changed its verified artifact identity")
        actual_result = None
        for repeat_index, (actual_row, expected_row) in enumerate(zip(
            rows, oracle["executions"]
        )):
            expected_id = test_identity
            if repeat_count > 1:
                expected_id = f"{expected_id}:round:{repeat_index + 1}"
            if (actual_row.get("id") != expected_id
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError("generic Test call did not complete under its inventory identity")
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError("generic Test returned the wrong result arity")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError("canonical VM generic Test result differs from Stage-0")
            if (not isinstance(actual, dict)
                    or actual.get("verdict_code") != 0
                    or actual.get("assertions") != 1
                    or actual.get("failures") != 0):
                raise AssertionError(f"generic Test did not return a passing TestResult: {actual}")
            if actual_result is None:
                actual_result = actual
            elif actual_result != actual:
                raise AssertionError("repeated generic Test invocation changed its result")

        return {
            "test_repository_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=app.TEST_ROOT, text=True
            ).strip(),
            "assertions_source": {
                "path": "native/mncs/test/assertions.mncs",
                "sha256": sha(assertions_source.encode("utf-8")),
            },
            "test_case_identity": test_identity,
            "generic_parameters": test_row["generic_params"],
            "input_signature": test_row["inputs"],
            "type_arguments": type_arguments,
            "input_byte_values": {
                "expected": first_byte_values,
                "actual": second_byte_values,
            },
            "generic_entrypoint": entrypoint,
            "verified_artifact_identity": built["artifact"]["identity"],
            "canonical_vm_artifact_identity": vm["artifact_id"],
            "canonical_vm_batch_count": len(rows),
            "repeat_results_match": True,
            "stage0_equivalent": True,
            "invalid_specialization_arity_rejected": True,
            "malformed_sequence_rejected": True,
            "typed_test_result": actual_result,
            "build_frontend_invocations": frontend_invocations(built),
            "build_process_metrics": build_metrics,
            "stage0_oracle_metrics": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss,
        }


def run_failure_isolation_test_batch() -> dict:
    """Keep a typed Test failure local while later inventory calls complete."""
    module = "tests.failure_isolation"
    source = """mncs 0.18;
module tests.failure_isolation;
use mncs.test.assertions;

fn selected_entry() -> (result: u64) { return 0; }
test failing_case() -> (result: TestResult) {
    return from_assertion(equals_i64(23, 24, 9301));
}
test passing_case() -> (result: TestResult) {
    return from_assertion(equals_i64(23, 23, 9302));
}
"""
    assertions_path = app.TEST_ROOT / "native/mncs/test/assertions.mncs"
    assertions_source = assertions_path.read_text(encoding="utf-8")
    modules = {"mncs.test.assertions": assertions_source}
    test_names = ["failing_case", "passing_case"]
    build_request = {
        "schema_version": "mncs.compiler.application-build-request/4",
        "target": "mncs-vm-0.1",
        "root_source_id": "tests/failure_isolation.mncs",
        "root_source": source,
        "callable_roots": [{"module": module, "function": "selected_entry"}],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "application-build-test-failure-isolation/1",
        "test_selection": {"kind": "all_source_cases"},
    }
    with tempfile.TemporaryDirectory(prefix="mncs-test-failure-isolation-") as raw:
        directory = Path(raw)
        probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send(
                {"compile_application": build_request}, timeout=180.0
            )
        finally:
            probe.close()
        inventory = require_inventory(
            built,
            set(test_names),
            expected_module=module,
            response_schema="mncs.compiler.application-build/7",
        )
        tests = inventory["tests"]
        if ([row.get("name") for row in tests] != test_names
                or frontend_invocations(built) != 1):
            raise AssertionError("failure-isolation Tests were not selected from one Language inventory")
        artifact = built.get("artifact", {}).get("value")
        if (built.get("status") != "verified"
                or built.get("unresolved_obligations") != []
                or not isinstance(artifact, dict)):
            raise AssertionError("failure-isolation Tests did not produce one verified VM artifact")

        repeat_count = 2
        oracle_calls = [{
            "schema_version": "0.1",
            "target": {"module": module, "function": row["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 100_000,
        } for _ in range(repeat_count) for row in tests]
        oracle_probe = app.Probe(directory / "oracle-cache", executable=PROBE)
        try:
            oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": source,
                "modules": modules,
                "calls": oracle_calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        expected_count = len(tests) * repeat_count
        if (not oracle.get("valid")
                or len(oracle.get("executions", [])) != expected_count):
            raise AssertionError("Stage-0 did not complete both rounds after the expected failed TestResult")

        vm, vm_wall, vm_peak_rss = execute_inventory_batch(
            artifact, inventory, repeat_count=repeat_count
        )
        rows = vm.get("results", [])
        if (vm.get("artifact_id") != artifact.get("artifact_id")
                or len(rows) != expected_count
                or built.get("artifact", {}).get("identity") != artifact.get("artifact_id")):
            raise AssertionError("failure-isolation batch changed artifact identity or call count")

        results_by_case: dict[str, list[dict]] = {name: [] for name in test_names}
        repeated_rows = [row for _ in range(repeat_count) for row in tests]
        for index, (test, actual_row, expected_row) in enumerate(zip(
            repeated_rows, rows, oracle["executions"]
        )):
            repeat_index = index // len(tests)
            expected_id = test["test_case_identity"]
            if repeat_count > 1:
                expected_id = f"{expected_id}:round:{repeat_index + 1}"
            if (actual_row.get("id") != expected_id
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError(f"Test failure escaped its result value: {actual_row}")
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError(f"TestResult returned the wrong arity for {test['name']}")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError(
                    f"canonical VM TestResult differs from Stage-0 for {test['name']}"
                )
            results_by_case[test["name"]].append(actual)

        failure = results_by_case["failing_case"]
        passed = results_by_case["passing_case"]
        if (len(failure) != repeat_count
                or any(result.get("verdict_code") != 1
                       or result.get("failure_code") != 9301
                       or result.get("assertions") != 1
                       or result.get("failures") != 1 for result in failure)
                or len(passed) != repeat_count
                or any(result.get("verdict_code") != 0
                       or result.get("failure_code") != 0
                       or result.get("assertions") != 1
                       or result.get("failures") != 0 for result in passed)
                or failure[0] != failure[1]
                or passed[0] != passed[1]):
            raise AssertionError("native Test failure or later passing result was not deterministic")

        return {
            "test_repository_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=app.TEST_ROOT, text=True
            ).strip(),
            "test_case_identities": {
                row["name"]: row["test_case_identity"] for row in tests
            },
            "verified_artifact_identity": built["artifact"]["identity"],
            "canonical_vm_artifact_identity": vm["artifact_id"],
            "canonical_vm_batch_count": len(rows),
            "repeat_round_count": repeat_count,
            "failing_result": failure[0],
            "passing_result_after_failure": passed[0],
            "failure_does_not_abort_later_case": True,
            "repeat_results_match": True,
            "stage0_equivalent": True,
            "build_frontend_invocations": frontend_invocations(built),
            "build_process_metrics": build_metrics,
            "stage0_oracle_metrics": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss,
        }


def run_interrupted_vm_recovery_batch() -> dict:
    """Recover valid Test calls after killing an active VM batch process."""
    module = "tests.interrupted_vm_recovery"
    source = """mncs 0.18;
module tests.interrupted_vm_recovery;
use mncs.test.assertions;

fn selected_entry() -> (result: u64) { return 0; }
test long_case() -> (result: TestResult) {
    iterate outer up_to 1024 carrying total: u64 = 0 {
        iterate inner up_to 1024 carrying count: u64 = 0 {
            next count = count + 1;
        }
        next total = total + count;
    }
    return from_assertion(equals_i64(1048576, total as i64, 9401));
}
test recover_case() -> (result: TestResult) {
    return from_assertion(equals_i64(23, 23, 9402));
}
"""
    assertions_path = app.TEST_ROOT / "native/mncs/test/assertions.mncs"
    assertions_source = assertions_path.read_text(encoding="utf-8")
    modules = {"mncs.test.assertions": assertions_source}
    test_names = ["long_case", "recover_case"]
    build_request = {
        "schema_version": "mncs.compiler.application-build-request/4",
        "target": "mncs-vm-0.1",
        "root_source_id": "tests/interrupted_vm_recovery.mncs",
        "root_source": source,
        "callable_roots": [{"module": module, "function": "selected_entry"}],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "application-build-interrupted-vm-recovery/1",
        "test_selection": {"kind": "all_source_cases"},
    }
    with tempfile.TemporaryDirectory(prefix="mncs-test-interrupted-vm-") as raw:
        directory = Path(raw)
        probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send(
                {"compile_application": build_request}, timeout=180.0
            )
        finally:
            probe.close()
        inventory = require_inventory(
            built,
            set(test_names),
            expected_module=module,
            response_schema="mncs.compiler.application-build/7",
        )
        tests = inventory["tests"]
        if ([row.get("name") for row in tests] != test_names
                or frontend_invocations(built) != 1):
            raise AssertionError("interruption recovery Tests were not selected from one Language inventory")
        artifact = built.get("artifact", {}).get("value")
        if (built.get("status") != "verified"
                or built.get("unresolved_obligations") != []
                or not isinstance(artifact, dict)):
            raise AssertionError("interruption recovery Tests did not produce one verified VM artifact")

        artifact_path = directory / "application.vm.json"
        calls_path = directory / "interrupted.calls.json"
        interrupted_output_path = directory / "interrupted.results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        interrupted_call = [{
            "id": "interrupted-long-case",
            "request": {
                "schema_version": "0.1",
                "target": {"module": module, "function": "long_case"},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 100_000_000,
            },
        }]
        calls_path.write_text(json.dumps(interrupted_call, sort_keys=True) + "\n", encoding="utf-8")
        interrupted_started = time.monotonic()
        process = subprocess.Popen(
            [str(app.VM_BIN), "batch", "--artifact", str(artifact_path),
             "--calls", str(calls_path), "--output", str(interrupted_output_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        was_running = False
        return_code = None
        try:
            time.sleep(0.02)
            was_running = process.poll() is None
            if was_running:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return_code = process.wait(timeout=5.0)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5.0)
        interrupted_wall = time.monotonic() - interrupted_started
        if (not was_running
                or return_code != -signal.SIGKILL
                or interrupted_output_path.exists()):
            raise AssertionError(
                "interrupted VM batch did not stop before publishing a result: "
                f"running={was_running}, returncode={return_code}, "
                f"result_exists={interrupted_output_path.exists()}"
            )

        recovery_calls = [{
            "id": f"{tests[1]['test_case_identity']}:recovery:{index + 1}",
            "request": {
                "schema_version": "0.1",
                "target": {"module": module, "function": "recover_case"},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 100_000,
            },
        } for index in range(2)]
        oracle_probe = app.Probe(directory / "oracle-cache", executable=PROBE)
        try:
            oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": source,
                "modules": modules,
                "calls": [{
                    "schema_version": "0.1",
                    "target": {"module": module, "function": "recover_case"},
                    "arguments": [],
                    "type_arguments": [],
                    "step_budget": 100_000,
                } for _ in range(2)],
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        if (not oracle.get("valid") or len(oracle.get("executions", [])) != 2):
            raise AssertionError("Stage-0 did not complete both post-interruption recovery calls")

        recovery_calls_path = directory / "recovery.calls.json"
        recovery_output_path = directory / "recovery.results.json"
        recovery_calls_path.write_text(
            json.dumps(recovery_calls, sort_keys=True) + "\n", encoding="utf-8"
        )
        recovery_started = time.monotonic()
        recovered = subprocess.run(
            ["/usr/bin/time", "-v", str(app.VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(recovery_calls_path),
             "--output", str(recovery_output_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        recovery_wall = time.monotonic() - recovery_started
        if recovered.returncode != 0 or not recovery_output_path.is_file():
            raise AssertionError(
                f"fresh VM process failed to recover on the same artifact: {recovered.stderr[-2000:]}"
            )
        rss_match = re.search(
            r"Maximum resident set size \(kbytes\): (\d+)", recovered.stderr
        )
        recovery_peak_rss = int(rss_match.group(1)) if rss_match else None
        vm = json.loads(recovery_output_path.read_text(encoding="utf-8"))
        results = vm.get("results", [])
        if (vm.get("artifact_id") != artifact.get("artifact_id")
                or len(results) != 2
                or built.get("artifact", {}).get("identity") != artifact.get("artifact_id")):
            raise AssertionError("post-interruption recovery changed artifact identity or call count")

        observed = []
        for actual_row, expected_row, request_value in zip(
            results, oracle["executions"], recovery_calls
        ):
            if (actual_row.get("id") != request_value["id"]
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError(f"post-interruption Test did not complete: {actual_row}")
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError("post-interruption TestResult returned the wrong arity")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError("post-interruption canonical VM result differs from Stage-0")
            if (actual.get("verdict_code") != 0
                    or actual.get("failure_code") != 0
                    or actual.get("assertions") != 1
                    or actual.get("failures") != 0):
                raise AssertionError(f"post-interruption Test did not return a passing result: {actual}")
            observed.append(actual)
        if observed[0] != observed[1]:
            raise AssertionError("post-interruption recovery results were not deterministic")

        return {
            "test_case_identities": {
                row["name"]: row["test_case_identity"] for row in tests
            },
            "verified_artifact_identity": built["artifact"]["identity"],
            "canonical_vm_artifact_identity": vm["artifact_id"],
            "build_frontend_invocations": frontend_invocations(built),
            "interruption": {
                "signal": "SIGKILL",
                "returncode": return_code,
                "process_was_active_before_signal": was_running,
                "result_file_published": interrupted_output_path.exists(),
                "wall_seconds": round(interrupted_wall, 6),
            },
            "recovery_call_count": len(results),
            "recovery_results_match": True,
            "recovery_results_deterministic": True,
            "stage0_equivalent": True,
            "recovered_test_result": observed[0],
            "build_process_metrics": build_metrics,
            "stage0_oracle_metrics": oracle_metrics,
            "recovery_batch_wall_seconds": round(recovery_wall, 6),
            "recovery_batch_peak_rss_kib": recovery_peak_rss,
        }


def run_real_mncs_test_suite() -> dict:
    module = "tests.self_suite"
    root_path = app.TEST_ROOT / "tests/self_suite.mncs"
    module_paths = {
        "mncs.test.assertions": app.TEST_ROOT / "native/mncs/test/assertions.mncs",
        "mncs.test.suite": app.TEST_ROOT / "native/mncs/test/suite.mncs",
        "mncs.test.generative": app.TEST_ROOT / "native/mncs/test/generative.mncs",
        "mncs.test.snapshot": app.TEST_ROOT / "native/mncs/test/snapshot.mncs",
        "mncs.test.runner": app.TEST_ROOT / "native/mncs/test/runner.mncs",
    }
    expected_names = {
        "arithmetic",
        "boolean",
        "task_lifecycle",
        "property_replay",
        "snapshot_witness",
        "skipped",
        "native_runner_policy",
    }
    if not root_path.is_file() or any(not path.is_file() for path in module_paths.values()):
        raise FileNotFoundError("the selected mncs-test self-suite or one of its MNCS libraries is missing")
    source = root_path.read_text(encoding="utf-8")
    module_sources = {
        name: path.read_text(encoding="utf-8") for name, path in module_paths.items()
    }
    language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    language_revision = language_lock["revision"]
    language_repo = None
    # The exact Compiler worktree can carry its pinned Language checkout next
    # to it, while the workspace's default Language checkout may have advanced
    # on a separate history. Select by the locked commit and repository
    # manifest instead of assuming the default sibling contains that object.
    language_candidates = [
        ROOT.parent / "mncs-language",
        app.WORKSPACE_ROOT / "mncs-language",
    ]
    seen_language_candidates = set()
    for candidate in language_candidates:
        try:
            resolved_candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved_candidate in seen_language_candidates:
            continue
        seen_language_candidates.add(resolved_candidate)
        manifest_path = resolved_candidate / ".mncs" / "project.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("repository") != "mncs-language":
            continue
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=resolved_candidate, capture_output=True, text=True,
        )
        expected_remote = language_lock.get("repository")
        if (remote.returncode != 0 or not isinstance(expected_remote, str)
                or remote.stdout.strip().removesuffix(".git")
                != expected_remote.removesuffix(".git")):
            continue
        pinned_object = subprocess.run(
            ["git", "cat-file", "-e", f"{language_revision}:library/std/task.mncs"],
            cwd=resolved_candidate, capture_output=True, text=True,
        )
        if pinned_object.returncode == 0:
            language_repo = resolved_candidate
            break
    if language_repo is None:
        raise AssertionError(
            f"no registered mncs-language checkout contains locked revision {language_revision}"
        )
    language_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=language_repo, text=True
    ).strip()
    task_source = subprocess.check_output(
        ["git", "show", f"{language_revision}:library/std/task.mncs"],
        cwd=language_repo,
        text=True,
    )
    module_sources["mncs.std.task.v1"] = task_source
    module_sources = dict(sorted(module_sources.items()))
    module_provenance = {
        name: {
            "repository": "mncs-test",
            "path": str(path.relative_to(app.TEST_ROOT)),
            "sha256": sha(module_sources[name].encode("utf-8")),
        }
        for name, path in module_paths.items()
    }
    module_provenance["mncs.std.task.v1"] = {
        "repository": "mncs-language",
        "revision": language_revision,
        "path": "library/std/task.mncs",
        "sha256": sha(task_source.encode("utf-8")),
    }
    with tempfile.TemporaryDirectory(prefix="mncs-test-real-self-suite-") as raw:
        directory = Path(raw)
        probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            build_request = request(
                source,
                [],
                root_source_id="mncs-test/tests/self_suite.mncs",
                modules=module_sources,
            )
            build_request["schema_version"] = "mncs.compiler.application-build-request/5"
            build_request["test_selection"] = {"kind": "all_source_cases"}
            built, build_metrics = probe.send(
                {"compile_application": build_request},
                timeout=180.0,
            )
        finally:
            probe.close()

        inventory = require_inventory(
            built,
            expected_names,
            expected_module=module,
            response_schema="mncs.compiler.application-build/8",
        )
        cold_fact_work = require_language_fact_v3(
            built, "cold real mncs-test self-suite build"
        )
        if cold_fact_work["produced_semantic_fact_count"] <= 0:
            raise AssertionError("real mncs-test self-suite produced no Language body facts")
        selection = built.get("test_case_selection", {})
        selected_callables = built.get("callable_scope", {}).get("callable_roots", [])
        if (selection.get("status") != "verified"
                or selection.get("selection") != {"kind": "all_source_cases"}
                or [row.get("test_case_identity") for row in selection.get("cases", [])]
                != [row.get("test_case_identity") for row in inventory["tests"]]
                or {(row.get("module"), row.get("function")) for row in selected_callables}
                != {(module, name) for name in expected_names}
                or any(row.get("status") != "verified" for row in selected_callables)):
            raise AssertionError("application-build/7 did not select the real self-suite by Language inventory")
        artifact = built.get("artifact", {}).get("value")
        if (built.get("cache", {}).get("status") != "miss"
                or frontend_invocations(built) != 1
                or not isinstance(artifact, dict)):
            raise AssertionError("real mncs-test self-suite did not produce one fresh verified artifact")
        artifact_callables = {
            (row.get("module"), row.get("name"))
            for row in artifact.get("callables", [])
        }
        selected = {(module, name) for name in expected_names}
        if not selected <= artifact_callables:
            raise AssertionError("real Test inventory roots are absent from the verified artifact")

        location_marker = "test arithmetic()"
        if source.count(location_marker) != 1:
            raise AssertionError("real mncs-test source no longer has one arithmetic Test declaration")
        comment_source = source.replace(
            location_marker,
            "// Language fact v3 source-location refresh\n" + location_marker,
            1,
        )
        comment_request = request(
            comment_source,
            [],
            root_source_id="mncs-test/tests/self_suite.mncs",
            modules=module_sources,
        )
        comment_request["schema_version"] = "mncs.compiler.application-build-request/5"
        comment_request["test_selection"] = {"kind": "all_source_cases"}
        comment_probe = app.Probe(
            directory / "program-cache",
            executable=PROBE,
            application_cache_dir=directory / "application-cache",
        )
        try:
            comment_build, comment_build_metrics = comment_probe.send(
                {"compile_application": comment_request}, timeout=180.0
            )
        finally:
            comment_probe.close()
        comment_inventory = require_inventory(
            comment_build,
            expected_names,
            expected_module=module,
            response_schema="mncs.compiler.application-build/8",
        )
        comment_fact_work = require_language_fact_v3(
            comment_build, "comment-edited real mncs-test self-suite build"
        )
        before_tests = {row["name"]: row for row in inventory["tests"]}
        after_tests = {row["name"]: row for row in comment_inventory["tests"]}
        reused_count = len(comment_fact_work["reused_semantic_facts"])
        if (
            comment_build.get("cache", {}).get("status") != "hit"
            or comment_build.get("cache", {}).get("reuse_basis")
            != "verified_application_executable_closure"
            or frontend_invocations(comment_build) != 1
            or comment_build.get("artifact", {}).get("identity")
            != built.get("artifact", {}).get("identity")
            or comment_fact_work.get("backend_artifact") != "reused"
            or not comment_fact_work["reused_semantic_facts"]
            or comment_fact_work["recomputed_semantic_facts"]
            or reused_count != comment_fact_work["produced_semantic_fact_count"]
            or comment_build.get("source_snapshot", {}).get("identity")
            == built.get("source_snapshot", {}).get("identity")
            or any(
                after_tests[name].get("test_case_identity")
                != before_tests[name].get("test_case_identity")
                or after_tests[name].get("source_span", {}).get("start", -1)
                <= before_tests[name].get("source_span", {}).get("start", -1)
                for name in before_tests
            )
        ):
            raise AssertionError(
                "real self-suite comment edit did not reuse all v3 facts and refresh current Test locations"
            )

        repeat_count = 2
        oracle_calls = [{
            "schema_version": "0.1",
            "target": {"module": row["module"], "function": row["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 200_000,
        } for _ in range(repeat_count) for row in inventory["tests"]]
        oracle_probe = app.Probe(directory / "oracle-cache", executable=PROBE)
        try:
            oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": source,
                "modules": module_sources,
                "calls": oracle_calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        expected_batch_count = len(inventory["tests"]) * repeat_count
        if (not oracle.get("valid")
                or len(oracle.get("executions", [])) != expected_batch_count):
            raise AssertionError("Stage-0 did not execute both rounds of the real mncs-test self-suite")

        vm, vm_wall, vm_peak_rss = execute_inventory_batch(
            artifact, inventory, step_budget=200_000, repeat_count=repeat_count
        )
        rows = vm.get("results", [])
        if (vm.get("artifact_id") != artifact.get("artifact_id")
                or len(rows) != expected_batch_count):
            raise AssertionError("real Test inventory batch changed artifact identity or repeated call count")
        if built.get("artifact", {}).get("identity") != artifact.get("artifact_id"):
            raise AssertionError("compiler build receipt and executable artifact identities differ")

        results: dict[str, dict] = {}
        test_rows = [row for _ in range(repeat_count) for row in inventory["tests"]]
        for index, (test, actual_row, expected_row) in enumerate(zip(
            test_rows, rows, oracle["executions"]
        )):
            repeat_index = index // len(inventory["tests"])
            expected_call_id = test["test_case_identity"]
            if repeat_count > 1:
                expected_call_id = f"{expected_call_id}:round:{repeat_index + 1}"
            if (actual_row.get("id") != expected_call_id
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError(f"real mncs-test case did not complete under its inventory identity: {actual_row}")
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError(f"real TestResult returned the wrong arity: {test['name']}")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError(
                    f"canonical VM TestResult differs from Stage-0 for {test['name']} round {repeat_index + 1}"
                )
            if (not isinstance(actual, dict)
                    or not {"verdict", "verdict_code", "failure_kind", "failure_code",
                            "assertions", "failures"} <= set(actual)):
                raise AssertionError(f"real Test case did not return a typed TestResult: {actual}")
            if repeat_index == 0 and test["name"] == "skipped":
                if (actual["verdict_code"] != 2 or actual["failure_code"] != 1006
                        or actual["assertions"] != 0 or actual["failures"] != 0):
                    raise AssertionError(f"native skip result changed its typed meaning: {actual}")
            elif repeat_index == 0 and (actual["verdict_code"] != 0
                    or actual["assertions"] != 1
                    or actual["failures"] != 0):
                raise AssertionError(f"real Test case did not return one passing TestResult: {actual}")
            if repeat_index == 0:
                results[test["name"]] = actual
            elif results.get(test["name"]) != actual:
                raise AssertionError(f"repeated native TestResult changed for {test['name']}: {actual}")

        selected_names = {"arithmetic", "skipped"}
        selected_identities = [
            row["test_case_identity"]
            for row in inventory["tests"]
            if row["name"] in selected_names
        ]
        if len(selected_identities) != len(selected_names):
            raise AssertionError("real mncs-test self-suite omitted an identity-selected case")
        selected_request = request(
            source,
            [],
            root_source_id="mncs-test/tests/self_suite.mncs",
            modules=module_sources,
        )
        selected_request["schema_version"] = "mncs.compiler.application-build-request/4"
        selected_request["test_selection"] = {
            "kind": "case_identities",
            "test_case_identities": selected_identities,
        }
        selected_probe = app.Probe(
            directory / "identity-selection-program-cache",
            executable=PROBE,
            application_cache_dir=directory / "identity-selection-application-cache",
        )
        try:
            selected_build, selected_build_metrics = selected_probe.send(
                {"compile_application": selected_request}, timeout=180.0
            )
        finally:
            selected_probe.close()
        selected_inventory = require_inventory(
            selected_build,
            expected_names,
            expected_module=module,
            response_schema="mncs.compiler.application-build/7",
        )
        selection = selected_build.get("test_case_selection", {})
        selected_cases = selection.get("cases")
        selected_roots = selected_build.get("callable_scope", {}).get("callable_roots", [])
        selected_root_pairs = {
            (row.get("module"), row.get("function")) for row in selected_roots
        }
        expected_selected_roots = {(module, name) for name in selected_names}
        selected_artifact = selected_build.get("artifact", {}).get("value")
        if (selected_build.get("status") != "verified"
                or frontend_invocations(selected_build) != 1
                or selection.get("status") != "verified"
                or selection.get("selection") != selected_request["test_selection"]
                or not isinstance(selected_cases, list)
                or {row.get("name") for row in selected_cases} != selected_names
                or selected_root_pairs != expected_selected_roots
                or any(row.get("status") != "verified" for row in selected_roots)
                or selected_inventory != inventory
                or not isinstance(selected_artifact, dict)):
            raise AssertionError(
                "identity-selected self-suite build did not retain the full inventory and exact selected roots"
            )

        require_inventory_rejection(
            "selection case with stale source location",
            lambda: execute_inventory_batch(
                selected_artifact,
                selected_inventory,
                selected_cases=[{**selected_cases[0], "source_span": {"start": -1}}],
            ),
        )
        unselected_case_identity = next(
            row["test_case_identity"]
            for row in selected_inventory["tests"]
            if row["name"] not in selected_names
        )
        require_inventory_rejection(
            "arguments for an unselected Test case",
            lambda: execute_inventory_batch(
                selected_artifact,
                selected_inventory,
                arguments_by_test_case={unselected_case_identity: []},
                selected_cases=selected_cases,
            ),
        )

        selected_oracle_calls = [{
            "schema_version": "0.1",
            "target": {"module": row["module"], "function": row["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 200_000,
        } for _ in range(repeat_count) for row in selected_cases]
        selected_oracle_probe = app.Probe(
            directory / "identity-selection-oracle-cache", executable=PROBE
        )
        try:
            selected_oracle, selected_oracle_metrics = selected_oracle_probe.send(
                {"project_oracle": {
                    "root": source,
                    "modules": module_sources,
                    "calls": selected_oracle_calls,
                }},
                timeout=180.0,
            )
        finally:
            selected_oracle_probe.close()
        if (not selected_oracle.get("valid")
                or len(selected_oracle.get("executions", []))
                != len(selected_cases) * repeat_count):
            raise AssertionError("Stage-0 did not execute every identity-selected Test call")
        selected_vm, selected_vm_wall, selected_vm_peak_rss = execute_inventory_batch(
            selected_artifact,
            selected_inventory,
            step_budget=200_000,
            repeat_count=repeat_count,
            selected_cases=selected_cases,
        )
        selected_vm_rows = selected_vm.get("results", [])
        selected_expected_rows = [
            row for _ in range(repeat_count) for row in selected_cases
        ]
        if (selected_vm.get("artifact_id") != selected_artifact.get("artifact_id")
                or len(selected_vm_rows) != len(selected_expected_rows)):
            raise AssertionError("identity-selected batch changed its artifact or exact call count")
        selected_results: dict[str, dict] = {}
        for index, (test, actual_row, expected_row) in enumerate(zip(
            selected_expected_rows,
            selected_vm_rows,
            selected_oracle["executions"],
        )):
            round_index = index // len(selected_cases)
            expected_call_id = test["test_case_identity"]
            if repeat_count > 1:
                expected_call_id = f"{expected_call_id}:round:{round_index + 1}"
            if (actual_row.get("id") != expected_call_id
                    or actual_row.get("outcome") != {"kind": "completed"}
                    or actual_row.get("record", {}).get("artifact_id")
                    != selected_artifact.get("artifact_id")):
                raise AssertionError(
                    f"identity-selected Test did not complete on the selected artifact: {actual_row}"
                )
            actual_values = actual_row.get("record", {}).get("returned", [])
            expected_values = expected_row.get("returned", [])
            if len(actual_values) != 1 or len(expected_values) != 1:
                raise AssertionError(f"identity-selected Test returned the wrong arity: {test['name']}")
            actual = app.decode_wire(app.vm_to_wire(actual_values[0]))
            expected = app.decode_wire(expected_values[0])
            if actual != expected:
                raise AssertionError(
                    f"identity-selected canonical VM result differs from Stage-0 for {test['name']}"
                )
            if round_index == 0:
                selected_results[test["name"]] = actual
            elif selected_results.get(test["name"]) != actual:
                raise AssertionError(f"identity-selected Test changed across rounds: {test['name']}")
        if (selected_results.get("arithmetic", {}).get("verdict_code") != 0
                or selected_results.get("skipped", {}).get("verdict_code") != 2):
            raise AssertionError("identity-selected real Test subset lost its pass/skip outcomes")

        test_revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=app.TEST_ROOT, text=True
        ).strip()
        artifact_identity = built["artifact"]["identity"]
        artifact_callables = {
            (row.get("module"), row.get("name")): row
            for row in artifact.get("callables", [])
        }
        provider_cases = []
        for test in inventory["tests"]:
            callable_row = artifact_callables.get((module, test["name"]))
            binding = callable_row.get("test_binding") if callable_row else None
            required_binding_fields = {
                "test_case_identity", "declaration_identity", "callable_identity",
                "signature_identity",
            }
            if (not isinstance(binding, dict)
                    or not required_binding_fields <= set(binding)
                    or binding.get("test_case_identity") != test["test_case_identity"]
                    or binding.get("declaration_identity") != test["declaration_identity"]):
                raise AssertionError(
                    f"compiler artifact binding does not match Test inventory for {test['name']}: "
                    f"{binding}"
                )
            provider_cases.append({
                "name": test["name"],
                "artifact_identity": artifact_identity,
                "test_case_identity": binding["test_case_identity"],
                "declaration_identity": binding["declaration_identity"],
                "callable_identity": binding["callable_identity"],
                "signature_identity": binding["signature_identity"],
                "native_result": results[test["name"]],
            })
        return {
            "test_repository_revision": test_revision,
            "language_repository_head": language_head,
            "language_locked_revision": language_revision,
            "root_source": {
                "path": "tests/self_suite.mncs",
                "sha256": sha(source.encode("utf-8")),
                "module": module,
            },
            "dependency_sources": module_provenance,
            "request_schema": "mncs.compiler.application-build-request/5",
            "response_schema": built.get("schema_version"),
            "inventory_schema": inventory.get("schema_version"),
            "frontend_invocations": frontend_invocations(built),
            "verification_work": built.get("cache", {}).get("verification_work"),
            "language_fact_v3_reuse": {
                "schema": comment_fact_work.get("language_verification_fact_schema"),
                "cold_facts_produced": cold_fact_work.get("produced_semantic_fact_count"),
                "cold_facts_recomputed": len(cold_fact_work.get("recomputed_semantic_facts", [])),
                "comment_edit_frontend_invocations": frontend_invocations(comment_build),
                "comment_edit_facts_produced": comment_fact_work.get("produced_semantic_fact_count"),
                "comment_edit_facts_reused": reused_count,
                "comment_edit_facts_recomputed": len(comment_fact_work["recomputed_semantic_facts"]),
                "comment_edit_reused_all_facts": (
                    reused_count == comment_fact_work.get("produced_semantic_fact_count")
                ),
                "comment_edit_backend_artifact": comment_fact_work.get("backend_artifact"),
                "comment_edit_artifact_identity": comment_build.get("artifact", {}).get("identity"),
                "test_case_identities_stable": all(
                    after_tests[name].get("test_case_identity")
                    == before_tests[name].get("test_case_identity")
                    for name in before_tests
                ),
                "test_source_spans_refreshed": all(
                    after_tests[name].get("source_span", {}).get("start", -1)
                    > before_tests[name].get("source_span", {}).get("start", -1)
                    for name in before_tests
                ),
                "comment_build_process_metrics": comment_build_metrics,
                "verification_work": comment_fact_work,
            },
            "test_case_identities": {
                row["name"]: row["test_case_identity"] for row in inventory["tests"]
            },
            "selected_artifact_identity": built["artifact"]["identity"],
            "canonical_vm_artifact_identity": vm["artifact_id"],
            "provider_integration_cases": provider_cases,
            "canonical_vm_batch_count": len(rows),
            "distinct_test_count": len(inventory["tests"]),
            "repeat_round_count": repeat_count,
            "repeat_results_match": True,
            "canonical_vm_results": results,
            "passing_test_count": sum(
                result["verdict_code"] == 0 for result in results.values()
            ),
            "skipped_test_count": sum(
                result["verdict_code"] == 2 for result in results.values()
            ),
            "stage0_equivalent": True,
            "build_process_metrics": build_metrics,
            "stage0_oracle_metrics": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss,
            "identity_selected_subset_batch": {
                "source_test_count": len(selected_inventory["tests"]),
                "selected_test_count": len(selected_cases),
                "selected_test_case_identities": [
                    row["test_case_identity"] for row in selected_cases
                ],
                "selected_roots": [
                    {"module": row.get("module"), "function": row.get("function")}
                    for row in selected_roots
                ],
                "frontend_invocations": frontend_invocations(selected_build),
                "verified_artifact_identity": selected_build["artifact"]["identity"],
                "canonical_vm_artifact_identity": selected_vm["artifact_id"],
                "canonical_vm_batch_count": len(selected_vm_rows),
                "repeat_round_count": repeat_count,
                "canonical_vm_results": selected_results,
                "stage0_equivalent": True,
                "stale_selection_case_rejected": True,
                "unselected_case_arguments_rejected": True,
                "build_process_metrics": selected_build_metrics,
                "stage0_oracle_metrics": selected_oracle_metrics,
                "canonical_vm_batch_wall_seconds": round(selected_vm_wall, 6),
                "canonical_vm_batch_peak_rss_kib": selected_vm_peak_rss,
            },
            "language_verified_facts_reused": reused_count,
        }



def main() -> None:
    global PROBE
    probe_build = None
    if not os.environ.get("MNCS_TEST_INVENTORY_PROBE_BIN"):
        probe_build = provider_tests.ensure_current_probe()
        PROBE = app.PROBE
    if not PROBE.is_file() or not app.VM_BIN.is_file():
        raise SystemExit(f"Stage-0 probe or canonical VM is missing: {PROBE}, {app.VM_BIN}")
    expected_pairs = {
        "mncs.compiler.application-build-request/1": "mncs.compiler.application-build/4",
        "mncs.compiler.application-build-request/2": "mncs.compiler.application-build/5",
        "mncs.compiler.application-build-request/3": "mncs.compiler.application-build/6",
        "mncs.compiler.application-build-request/4": "mncs.compiler.application-build/7",
        "mncs.compiler.application-build-request/5": "mncs.compiler.application-build/8",
        "mncs.compiler.application-build-request/6": "mncs.compiler.application-build/9",
    }
    if provider.RESPONSE_SCHEMA_BY_REQUEST != expected_pairs:
        raise AssertionError("Environment provider does not accept all versioned build contracts")
    manifest = json.loads((ROOT / ".mncs/project.json").read_text(encoding="utf-8"))
    contract_rows = [
        row for row in manifest.get("contracts", {}).get("provides", [])
        if isinstance(row, dict) and row.get("contract") == "native-application-build"
    ]
    if (len(contract_rows) != 1 or contract_rows[0].get("version") != "9"
            or contract_rows[0].get("artifact_contract", {}).get("request_schema")
            != "mncs.compiler.application-build-request/6"):
        raise AssertionError("Compiler manifest does not advertise application-build/9")
    provider_tests.validate_environment_contract_descriptor()

    with tempfile.TemporaryDirectory(
        prefix=".application-test-inventory-", dir=ROOT
    ) as raw:
        directory = Path(raw)
        policy_cache_isolation = verify_build_assumption_cache_isolation(
            directory / "policy-cache-isolation"
        )
        program_cache = directory / "program-cache"
        application_cache = directory / "application-cache"

        probe = app.Probe(program_cache, executable=PROBE,
                          application_cache_dir=application_cache)
        try:
            cold, cold_metrics = probe.send({"compile_application": request(SOURCE)}, timeout=180.0)
        finally:
            probe.close()
        cold_inventory = require_inventory(cold)
        if (cold.get("cache", {}).get("status") != "miss"
                or frontend_invocations(cold) != 1):
            raise AssertionError("cold inventory build did not report one frontend invocation")

        # The caller selects its production entry and a bounded set of Test
        # declarations in one application-build/6 request. The returned
        # Language inventory supplies the stable call identifiers for a single
        # canonical VM batch over that exact artifact.
        inventory_roots = [
            {"module": MODULE, "function": "selected_entry"},
            {"module": MODULE, "function": "first_case"},
            {"module": MODULE, "function": "second_case"},
        ]
        runner_dir = directory / "inventory-runner"
        runner_dir.mkdir()
        runner_probe = app.Probe(
            runner_dir / "program-cache",
            executable=PROBE,
            application_cache_dir=runner_dir / "application-cache",
        )
        try:
            runner_build, runner_build_metrics = runner_probe.send(
                {"compile_application": request(SOURCE, inventory_roots)}, timeout=180.0
            )
        finally:
            runner_probe.close()
        runner_inventory = require_inventory(runner_build)
        runner_artifact = runner_build.get("artifact", {}).get("value")
        if (runner_build.get("cache", {}).get("status") != "miss"
                or frontend_invocations(runner_build) != 1
                or not isinstance(runner_artifact, dict)):
            raise AssertionError("inventory runner did not build one verified artifact with one frontend")
        expected_test_names = {"first_case", "second_case"}
        runner_tests = runner_inventory.get("tests", [])
        if {row.get("name") for row in runner_tests} != expected_test_names:
            raise AssertionError("runner inventory changed the selected source Test set")
        artifact_callables = {
            (row.get("module"), row.get("name"))
            for row in runner_artifact.get("callables", [])
        }
        if not set((MODULE, name) for name in expected_test_names) <= artifact_callables:
            raise AssertionError("selected inventory tests are missing from the verified artifact")

        oracle_calls = [{
            "schema_version": "0.1",
            "target": {"module": row["module"], "function": row["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 100_000,
        } for row in runner_tests]
        oracle_probe = app.Probe(runner_dir / "oracle-cache", executable=PROBE)
        try:
            inventory_oracle, inventory_oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": SOURCE,
                "modules": {},
                "calls": oracle_calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        if (not inventory_oracle.get("valid")
                or len(inventory_oracle.get("executions", [])) != len(runner_tests)):
            raise AssertionError("Stage-0 Test inventory oracle did not execute the selected cases")
        inventory_vm, inventory_vm_wall, inventory_vm_peak_rss = execute_inventory_batch(
            runner_artifact, runner_inventory
        )
        inventory_vm_rows = inventory_vm.get("results", [])
        if (len(inventory_vm_rows) != len(runner_tests)
                or inventory_vm.get("artifact_id") != runner_artifact.get("artifact_id")):
            raise AssertionError("canonical VM batch did not retain the selected artifact and Test count")
        inventory_results: dict[str, int] = {}
        for test_row, vm_row, oracle_row in zip(
            runner_tests, inventory_vm_rows, inventory_oracle["executions"]
        ):
            if (vm_row.get("id") != test_row["test_case_identity"]
                    or vm_row.get("outcome") != {"kind": "completed"}
                    or vm_row.get("record", {}).get("artifact_id") != runner_artifact.get("artifact_id")):
                raise AssertionError(f"inventory-selected Test did not complete: {vm_row}")
            actual_rows = vm_row.get("record", {}).get("returned", [])
            expected_rows = oracle_row.get("returned", [])
            if len(actual_rows) != 1 or len(expected_rows) != 1:
                raise AssertionError(f"inventory-selected Test returned the wrong arity: {test_row['name']}")
            actual = app.decode_wire(app.vm_to_wire(actual_rows[0]))
            expected = app.decode_wire(expected_rows[0])
            if actual != expected:
                raise AssertionError(f"canonical VM differs from Stage-0 for {test_row['name']}")
            inventory_results[test_row["name"]] = actual
        if inventory_results != {"first_case": 1, "second_case": 2}:
            raise AssertionError(f"selected inventory Test results were unexpected: {inventory_results}")

        parameterized_test_batch = run_parameterized_test_case()
        generic_test_batch = run_generic_test_case()
        failure_isolation_test_batch = run_failure_isolation_test_batch()
        interrupted_vm_recovery_batch = run_interrupted_vm_recovery_batch()
        real_mncs_test_suite = run_real_mncs_test_suite()

        warm_probe = app.Probe(program_cache, executable=PROBE,
                               application_cache_dir=application_cache)
        try:
            warm, warm_metrics = warm_probe.send(
                {"compile_application": request(SOURCE)}, timeout=90.0
            )
        finally:
            warm_probe.close()
        warm_inventory = require_inventory(warm)
        if (warm.get("cache", {}).get("status") != "hit"
                or frontend_invocations(warm) != 0
                or warm.get("artifact", {}).get("identity") != cold.get("artifact", {}).get("identity")
                or warm_inventory != cold_inventory):
            raise AssertionError("fresh-process exact reuse changed the inventory or artifact")

        edit_probe = app.Probe(program_cache, executable=PROBE,
                               application_cache_dir=application_cache)
        try:
            comment_source = SOURCE.replace(
                "test second_case()", "// comment shifts source locations\ntest second_case()", 1
            )
            comment_response, comment_metrics = edit_probe.send(
                {"compile_application": request(comment_source)}, timeout=180.0
            )
            comment_inventory = require_inventory(comment_response)
            before = {row["name"]: row for row in cold_inventory["tests"]}
            after = {row["name"]: row for row in comment_inventory["tests"]}
            if (after["second_case"]["source_span"]["start"]
                    <= before["second_case"]["source_span"]["start"]):
                raise AssertionError("comment edit did not refresh current source locations")
            if any(after[name]["test_case_identity"] != before[name]["test_case_identity"]
                   for name in before):
                raise AssertionError("comment-only edit changed semantic Test identities")
            if frontend_invocations(comment_response) != 1:
                raise AssertionError("comment edit did not obtain a fresh root-source inventory")
            comment_work = comment_response.get("cache", {}).get("verification_work", {})
            if (comment_response.get("cache", {}).get("status") != "hit"
                    or comment_response.get("cache", {}).get("reuse_basis")
                    != "verified_application_executable_closure"
                    or comment_work.get("backend_artifact") != "reused"):
                raise AssertionError("comment edit did not reuse its unchanged executable closure")

            body_source = SOURCE.replace(
                "test second_case() -> (result: u64) { return 2; }",
                "test second_case() -> (result: u64) { return 9; }",
                1,
            )
            body_probe = app.Probe(program_cache, executable=PROBE,
                                   application_cache_dir=application_cache)
            try:
                body_response, body_metrics = body_probe.send(
                    {"compile_application": request(body_source)}, timeout=180.0
                )
            finally:
                body_probe.close()
            body_inventory = require_inventory(body_response)
            body_tests = {row["name"]: row for row in body_inventory["tests"]}
            if (body_tests["second_case"]["test_case_identity"]
                    == after["second_case"]["test_case_identity"]
                    or body_tests["first_case"]["test_case_identity"]
                    != after["first_case"]["test_case_identity"]):
                raise AssertionError("test-body edit did not invalidate only its own test identity")
            comment_closure = comment_response.get("dependency_graph", {}).get(
                "application_executable_closure_identity"
            )
            body_closure = body_response.get("dependency_graph", {}).get(
                "application_executable_closure_identity"
            )
            if not comment_closure or body_closure != comment_closure:
                raise AssertionError("unselected Test body edit changed the selected executable closure")
            if frontend_invocations(body_response) != 1:
                raise AssertionError("unselected root Test body edit did not run the frontend")
            if body_metrics.get("request_sequence") != 1:
                raise AssertionError("root Test body edit was not checked in a fresh process")
            body_work = body_response.get("cache", {}).get("verification_work", {})
            if (body_response.get("cache", {}).get("status") != "hit"
                    or body_response.get("cache", {}).get("reuse_basis")
                    != "verified_application_executable_closure"
                    or body_work.get("backend_artifact") != "reused"
                    or body_work.get("ssa_verification") != "reused_verified_vm_artifact"
                    or body_response.get("artifact", {}).get("identity")
                    != comment_response.get("artifact", {}).get("identity")):
                raise AssertionError("unselected Test body edit did not reuse its verified VM artifact")

            invalid_source = body_source.replace("return 9;", "return missing_value;", 1)
            invalid_response, invalid_metrics = edit_probe.send(
                {"compile_application": request(invalid_source)}, timeout=180.0
            )
            if (invalid_response.get("status") != "rejected"
                    or invalid_response.get("artifact") is not None
                    or invalid_response.get("source_test_inventory") is not None
                    or not invalid_response.get("diagnostics")
                    or frontend_invocations(invalid_response) != 1):
                raise AssertionError("invalid source reused prior artifact or Test inventory success")

            no_tests_source = SOURCE.split("test second_case()", maxsplit=1)[0]
            empty_response, empty_metrics = edit_probe.send(
                {"compile_application": request(no_tests_source)}, timeout=180.0
            )
            empty_inventory = require_inventory(empty_response, expected_names=set())
            if empty_inventory.get("tests") != [] or frontend_invocations(empty_response) != 1:
                raise AssertionError("valid source without tests did not return a fresh empty inventory")
        finally:
            edit_probe.close()

        provider_request_path = directory / "provider-request.json"
        provider_tests.write_json(provider_request_path, request(SOURCE))
        provider_output_dir = directory / "provider-output"
        provider_output_dir.mkdir()
        provider_cache_dir = directory / "provider-cache"
        provider_summary, provider_response, provider_elapsed, provider_metrics = (
            provider_tests.invoke(
                [
                    "--request-file", provider_request_path.relative_to(ROOT).as_posix(),
                    "--cache-dir", provider_cache_dir.relative_to(ROOT).as_posix(),
                ],
                output_dir=provider_output_dir,
                cache_dir=provider_cache_dir,
            )
        )
        provider_inventory = require_inventory(provider_response)
        if (provider_summary.get("response_schema") != provider_response["schema_version"]
                or provider_response.get("schema_version")
                != "mncs.compiler.application-build/6"
                or provider_inventory != cold_inventory
                or not (provider_output_dir / "native-application-build.json").is_file()):
            raise AssertionError("application-build provider did not publish the /6 inventory result")

        selection_request = request(SOURCE)
        selection_request["schema_version"] = "mncs.compiler.application-build-request/4"
        selection_request["test_selection"] = {"kind": "all_source_cases"}
        selection_request_path = directory / "provider-test-selection-request.json"
        provider_tests.write_json(selection_request_path, selection_request)
        selection_output_dir = directory / "provider-test-selection-output"
        selection_output_dir.mkdir()
        selection_cache_dir = directory / "provider-test-selection-cache"
        selection_summary, selection_response, selection_elapsed, selection_metrics = (
            provider_tests.invoke(
                [
                    "--request-file", selection_request_path.relative_to(ROOT).as_posix(),
                    "--cache-dir", selection_cache_dir.relative_to(ROOT).as_posix(),
                ],
                output_dir=selection_output_dir,
                cache_dir=selection_cache_dir,
            )
        )
        selection_rows = selection_response.get("test_case_selection", {}).get("cases", [])
        selection_roots = {
            (row.get("module"), row.get("function"))
            for row in selection_response.get("callable_scope", {}).get("callable_roots", [])
        }
        if (selection_summary.get("response_schema")
                != "mncs.compiler.application-build/7"
                or selection_response.get("status") != "verified"
                or {row.get("name") for row in selection_rows}
                != {"first_case", "second_case"}
                or not { (MODULE, "first_case"), (MODULE, "second_case"), (MODULE, "selected_entry") }
                <= selection_roots
                or not (selection_output_dir / "native-application-build.json").is_file()):
            raise AssertionError("application-build provider did not publish the /7 identity-selection result")

        report = {
            "schema_version": 1,
            "kind": "mncs.compiler.application-build-source-test-inventory-proof",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler": {
                "revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "branch": subprocess.check_output(
                    ["git", "branch", "--show-current"], cwd=ROOT, text=True
                ).strip(),
                "probe_sha256": sha(PROBE.read_bytes()),
                "probe_build": probe_build,
                "harness_sha256": sha(Path(__file__).read_bytes()),
                "working_tree_patch_sha256": sha(subprocess.check_output(
                    ["git", "diff", "--binary", "HEAD"], cwd=ROOT
                )),
                "language_revision": json.loads(
                    (ROOT / "mncs-language.lock.json").read_text()
                )["revision"],
            },
            "provider_adapter": {
                "response_schema": provider_response["schema_version"],
                "source_test_count": len(provider_inventory["tests"]),
                "wall_seconds": round(provider_elapsed, 6),
                "process_tree_metrics": provider_metrics,
            },
            "identity_selection_provider_adapter": {
                "response_schema": selection_response["schema_version"],
                "selected_test_count": len(selection_rows),
                "selected_test_case_identities": [
                    row.get("test_case_identity") for row in selection_rows
                ],
                "wall_seconds": round(selection_elapsed, 6),
                "process_tree_metrics": selection_metrics,
            },
            "build_assumption_cache_isolation": policy_cache_isolation,
            "parameterized_test_batch": parameterized_test_batch,
            "generic_test_batch": generic_test_batch,
            "failure_isolation_test_batch": failure_isolation_test_batch,
            "interrupted_vm_recovery_batch": interrupted_vm_recovery_batch,
            "real_mncs_test_self_suite_batch": real_mncs_test_suite,
            "inventory_selected_batch": {
                "build_frontend_invocations": frontend_invocations(runner_build),
                "inventory_test_count": len(runner_tests),
                "inventory_test_case_identities": {
                    row["name"]: row["test_case_identity"] for row in runner_tests
                },
                "verified_artifact_identity": runner_build["artifact"]["identity"],
                "canonical_vm_artifact_identity": inventory_vm["artifact_id"],
                "canonical_vm_batch_count": len(inventory_vm_rows),
                "canonical_vm_results": inventory_results,
                "stage0_equivalent": True,
                "build_process_metrics": runner_build_metrics,
                "stage0_oracle_metrics": inventory_oracle_metrics,
                "canonical_vm_batch_wall_seconds": round(inventory_vm_wall, 6),
                "canonical_vm_batch_peak_rss_kib": inventory_vm_peak_rss,
            },
            "contract": {
                "request_schema": "mncs.compiler.application-build-request/4",
                "response_schema": cold["schema_version"],
                "identity_selection_request_schema": selection_request["schema_version"],
                "identity_selection_response_schema": selection_response["schema_version"],
                "identity_selection_schema": selection_response.get(
                    "test_case_selection", {}
                ).get("schema_version"),
                "source_test_inventory_schema": cold_inventory["schema_version"],
                "source_test_inventory_scope": cold_inventory["scope"],
                "source_artifact_identity": cold_inventory["source_artifact_identity"],
                "subject_identity": cold_inventory["subject_identity"],
                "subject_fingerprint": cold_inventory["subject_fingerprint"],
                "test_case_identities": {
                    row["name"]: row["test_case_identity"]
                    for row in cold_inventory["tests"]
                },
                "selected_artifact_identity": cold["artifact"]["identity"],
            },
            "checks": {
                "one_frontend_supplies_build_and_inventory": (
                    frontend_invocations(cold) == 1
                    and len(cold_inventory["tests"]) == 2
                ),
                "fresh_process_exact_hit_preserves_inventory": (
                    warm.get("cache", {}).get("status") == "hit"
                    and frontend_invocations(warm) == 0
                    and warm_inventory == cold_inventory
                ),
                "build_assumptions_isolate_real_application_cache_entries": (
                    len(set(policy_cache_isolation["application_cache_keys"].values())) == 3
                    and policy_cache_isolation["fresh_process_exact_hit"]["status"] == "hit"
                    and policy_cache_isolation["fresh_process_exact_hit"][
                        "front_end_invocations"
                    ] == 0
                ),
                "comment_edit_refreshes_locations_only": True,
                "unselected_test_body_changes_only_its_case_identity": True,
                "unselected_test_edit_preserves_executable_closure": (
                    body_closure == comment_closure
                ),
                "unselected_root_test_edit_artifact_identity": {
                    "before": comment_response.get("artifact", {}).get("identity"),
                    "after": body_response.get("artifact", {}).get("identity"),
                    "reused": body_response.get("artifact", {}).get("identity")
                    == comment_response.get("artifact", {}).get("identity"),
                    "reuse_basis": body_response.get("cache", {}).get("reuse_basis"),
                    "ssa_verification": body_work.get("ssa_verification"),
                    "fresh_process": body_metrics.get("request_sequence") == 1,
                },
                "fresh_process_changed_root_reuses_verified_artifact": (
                    body_metrics.get("request_sequence") == 1
                    and body_work.get("backend_artifact") == "reused"
                    and body_work.get("ssa_verification") == "reused_verified_vm_artifact"
                ),
                "comment_edit_reuses_executable_closure": (
                    comment_response.get("cache", {}).get("reuse_basis")
                    == "verified_application_executable_closure"
                    and comment_work.get("backend_artifact") == "reused"
                ),
                "invalid_source_suppresses_inventory_and_artifact": True,
                "valid_source_without_tests_returns_empty_inventory": True,
                "provider_adapter_publishes_inventory_response": (
                    provider_summary.get("response_schema")
                    == "mncs.compiler.application-build/6"
                    and provider_inventory == cold_inventory
                ),
                "provider_adapter_publishes_identity_selected_tests": (
                    selection_summary.get("response_schema")
                    == "mncs.compiler.application-build/7"
                    and len(selection_rows) == 2
                    and (MODULE, "first_case") in selection_roots
                    and (MODULE, "second_case") in selection_roots
                ),
                "one_build_emits_artifact_inventory_and_selected_test_callables": (
                    frontend_invocations(runner_build) == 1
                    and len(runner_tests) == 2
                    and set((MODULE, name) for name in expected_test_names) <= artifact_callables
                ),
                "inventory_ids_drive_one_batch_on_same_artifact": (
                    len(inventory_vm_rows) == len(runner_tests)
                    and all(vm.get("id") == test["test_case_identity"]
                            for vm, test in zip(inventory_vm_rows, runner_tests))
                    and inventory_vm.get("artifact_id") == runner_artifact.get("artifact_id")
                ),
                "selected_test_batch_matches_stage0": True,
                "typed_test_inputs_follow_language_inventory_and_match_stage0": (
                    parameterized_test_batch["input_signature"]
                    == [
                        {"name": "expected", "type": "u64"},
                        {"name": "actual", "type": "u64"},
                        {"name": "signed", "type": "i64"},
                        {"name": "enabled", "type": "bool"},
                        {"name": "marker", "type": "byte"},
                    ]
                    and parameterized_test_batch["input_values"]
                    == {
                        "expected": 23,
                        "actual": 23,
                        "signed": -17,
                        "enabled": True,
                        "marker": 255,
                    }
                    and parameterized_test_batch["canonical_vm_batch_count"] == 2
                    and parameterized_test_batch["repeat_results_match"]
                    and parameterized_test_batch["stage0_equivalent"]
                    and parameterized_test_batch["invalid_argument_count_rejected"]
                    and parameterized_test_batch["invalid_u64_wire_type_rejected"]
                    and parameterized_test_batch["invalid_i64_wire_type_rejected"]
                    and parameterized_test_batch["out_of_range_i64_rejected"]
                    and parameterized_test_batch["invalid_bool_wire_value_rejected"]
                    and parameterized_test_batch["out_of_range_byte_rejected"]
                    and parameterized_test_batch["typed_test_result"]["verdict_code"] == 0
                    and parameterized_test_batch["typed_test_result"]["expected"] == 1
                    and parameterized_test_batch["typed_test_result"]["actual"] == 1
                ),
                "generic_test_seed_resolves_to_a_native_entrypoint": (
                    generic_test_batch["generic_parameters"]
                    == [
                        {"kind": "nat", "name": "N"},
                        {"kind": "nat", "name": "M"},
                    ]
                    and generic_test_batch["type_arguments"]
                    == [
                        {"kind": "nat", "value": 2},
                        {"kind": "nat", "value": 3},
                    ]
                    and generic_test_batch["generic_entrypoint"]["args_spellings"] == ["2", "3"]
                    and generic_test_batch["input_signature"] == [
                        {"name": "expected", "type": "[byte; N]"},
                        {"name": "actual", "type": "[byte; M]"},
                    ]
                    and len(generic_test_batch["input_byte_values"]["expected"]) == 2
                    and len(generic_test_batch["input_byte_values"]["actual"]) == 3
                    and generic_test_batch["canonical_vm_batch_count"] == 2
                    and generic_test_batch["repeat_results_match"]
                    and generic_test_batch["stage0_equivalent"]
                    and generic_test_batch["invalid_specialization_arity_rejected"]
                    and generic_test_batch["malformed_sequence_rejected"]
                    and generic_test_batch["typed_test_result"]["verdict_code"] == 0
                ),
                "native_test_failure_does_not_abort_later_inventory_cases": (
                    failure_isolation_test_batch["build_frontend_invocations"] == 1
                    and failure_isolation_test_batch["canonical_vm_batch_count"] == 4
                    and failure_isolation_test_batch["repeat_round_count"] == 2
                    and failure_isolation_test_batch["failing_result"]["verdict_code"] == 1
                    and failure_isolation_test_batch["failing_result"]["failure_code"] == 9301
                    and failure_isolation_test_batch["passing_result_after_failure"]["verdict_code"] == 0
                    and failure_isolation_test_batch["failure_does_not_abort_later_case"]
                    and failure_isolation_test_batch["repeat_results_match"]
                    and failure_isolation_test_batch["stage0_equivalent"]
                ),
                "interrupted_vm_batch_publishes_no_result_and_recovers_same_artifact": (
                    interrupted_vm_recovery_batch["build_frontend_invocations"] == 1
                    and interrupted_vm_recovery_batch["interruption"]["signal"] == "SIGKILL"
                    and interrupted_vm_recovery_batch["interruption"]["process_was_active_before_signal"]
                    and interrupted_vm_recovery_batch["interruption"]["returncode"] == -signal.SIGKILL
                    and not interrupted_vm_recovery_batch["interruption"]["result_file_published"]
                    and interrupted_vm_recovery_batch["recovery_call_count"] == 2
                    and interrupted_vm_recovery_batch["verified_artifact_identity"]
                    == interrupted_vm_recovery_batch["canonical_vm_artifact_identity"]
                    and interrupted_vm_recovery_batch["recovery_results_match"]
                    and interrupted_vm_recovery_batch["recovery_results_deterministic"]
                    and interrupted_vm_recovery_batch["stage0_equivalent"]
                ),
                "real_mncs_test_self_suite_runs_from_compiler_inventory": (
                    real_mncs_test_suite["frontend_invocations"] == 1
                    and real_mncs_test_suite["canonical_vm_batch_count"] == 14
                    and real_mncs_test_suite["distinct_test_count"] == 7
                    and real_mncs_test_suite["repeat_round_count"] == 2
                    and real_mncs_test_suite["repeat_results_match"]
                    and real_mncs_test_suite["passing_test_count"] == 6
                    and real_mncs_test_suite["skipped_test_count"] == 1
                    and real_mncs_test_suite["stage0_equivalent"]
                ),
                "real_mncs_test_self_suite_returns_typed_results": all(
                    {"verdict", "verdict_code", "failure_kind", "failure_code",
                     "assertions", "failures"} <= set(result)
                    for result in real_mncs_test_suite["canonical_vm_results"].values()
                ),
                "real_mncs_test_self_suite_reuses_language_v3_facts_and_refreshes_locations": (
                    real_mncs_test_suite["language_fact_v3_reuse"]["schema"]
                    == "mncs.language-verification-fact/3"
                    and real_mncs_test_suite["language_fact_v3_reuse"][
                        "comment_edit_reused_all_facts"
                    ]
                    and real_mncs_test_suite["language_fact_v3_reuse"][
                        "test_case_identities_stable"
                    ]
                    and real_mncs_test_suite["language_fact_v3_reuse"][
                        "test_source_spans_refreshed"
                    ]
                ),
                "real_mncs_test_identity_selection_executes_only_requested_cases": (
                    real_mncs_test_suite["identity_selected_subset_batch"][
                        "source_test_count"
                    ] == 7
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "selected_test_count"
                    ] == 2
                    and {
                        row["function"]
                        for row in real_mncs_test_suite["identity_selected_subset_batch"][
                            "selected_roots"
                        ]
                    } == {"arithmetic", "skipped"}
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "frontend_invocations"
                    ] == 1
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "canonical_vm_batch_count"
                    ] == 4
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "repeat_round_count"
                    ] == 2
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "canonical_vm_results"
                    ]["arithmetic"]["verdict_code"] == 0
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "canonical_vm_results"
                    ]["skipped"]["verdict_code"] == 2
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "verified_artifact_identity"
                    ] == real_mncs_test_suite["identity_selected_subset_batch"][
                        "canonical_vm_artifact_identity"
                    ]
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "stage0_equivalent"
                    ]
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "stale_selection_case_rejected"
                    ]
                    and real_mncs_test_suite["identity_selected_subset_batch"][
                        "unselected_case_arguments_rejected"
                    ]
                ),
                "language_verified_facts_reused": real_mncs_test_suite[
                    "language_verified_facts_reused"
                ],
            },
            "measurements": {
                "cold_build": cold_metrics,
                "fresh_process_exact_hit": warm_metrics,
                "comment_edit": comment_metrics,
                "unselected_test_body_edit": body_metrics,
                "invalid_source": invalid_metrics,
                "valid_source_without_tests": empty_metrics,
                "identity_selection_provider": selection_metrics,
                "parameterized_test_build": parameterized_test_batch["build_process_metrics"],
                "parameterized_test_oracle": parameterized_test_batch["stage0_oracle_metrics"],
                "generic_test_build": generic_test_batch["build_process_metrics"],
                "generic_test_oracle": generic_test_batch["stage0_oracle_metrics"],
                "failure_isolation_test_build": failure_isolation_test_batch["build_process_metrics"],
                "failure_isolation_test_oracle": failure_isolation_test_batch["stage0_oracle_metrics"],
                "interrupted_vm_recovery_build": interrupted_vm_recovery_batch["build_process_metrics"],
                "interrupted_vm_recovery_oracle": interrupted_vm_recovery_batch["stage0_oracle_metrics"],
                "real_self_suite_identity_selected_build": real_mncs_test_suite[
                    "identity_selected_subset_batch"
                ]["build_process_metrics"],
                "real_self_suite_identity_selected_oracle": real_mncs_test_suite[
                    "identity_selected_subset_batch"
                ]["stage0_oracle_metrics"],
                "real_self_suite_comment_edit": real_mncs_test_suite[
                    "language_fact_v3_reuse"
                ]["comment_build_process_metrics"],
            },
            "verification_work": {
                "cold_build": cold.get("cache", {}).get("verification_work"),
                "fresh_process_exact_hit": warm.get("cache", {}).get("verification_work"),
                "comment_edit": comment_response.get("cache", {}).get("verification_work"),
                "unselected_test_body_edit": body_response.get("cache", {}).get("verification_work"),
                "invalid_source": invalid_response.get("cache", {}).get("verification_work"),
                "valid_source_without_tests": empty_response.get("cache", {}).get("verification_work"),
                "real_self_suite_comment_edit": real_mncs_test_suite[
                    "language_fact_v3_reuse"
                ]["verification_work"],
            },
            "scope": (
                "The Compiler returns the Language-owned source_module test inventory from the same "
                "frontend result used to verify callable roots and emit application-build/7. A fresh "
                "process can locate a prior artifact candidate across root snapshots; changed source "
                "still runs the full frontend before executable-closure reuse. Exact fresh-process "
                "reuse preserves the inventory; edits refresh source spans or the "
                "affected test identity, and invalid input publishes neither inventory nor artifact. "
                "A selected zero-argument source Test subset can be emitted with the production "
                "entry, selected from the returned inventory, and executed in one canonical VM batch "
                "with Stage-0 agreement. The real seven-case mncs-test self-suite also selects "
                "arithmetic and skipped by exact Language-issued identities, retains the full source "
                "inventory, and executes only those two selected callables twice on the emitted "
                "artifact with Stage-0 agreement; stale selection rows and arguments for unselected "
                "cases are rejected before dispatch. A bounded parameterized Test slice binds two typed u64 "
                "inputs, one signed i64, one bool, and one byte input to the Language-issued signature and repeats "
                "its typed TestResult "
                "through the same artifact with Stage-0 agreement; wrong arity and integer wire type "
                "are rejected before VM dispatch. The batch adapter accepts a generic "
                "Test only when its seed resolves to a concrete callable. The Compiler removes the "
                "generic template from executable exports, lowers its verified seeded specialization, "
                "and admits the selected Test when the exact generic-entrypoint spelling is present. "
                "A generic Test with independent Nat bounds 2 and 3 executes twice by inventory "
                "identity with two typed byte inputs and matches Stage-0. Wrong specialization arity "
                "and malformed sequence wire values are rejected. A failure-isolation batch then "
                "runs a failing typed TestResult followed by "
                "a passing case, twice on the same artifact; all four results complete and match "
                "Stage-0. A live canonical VM process is then killed during a bounded inventory "
                "Test call. It publishes no result file; a fresh VM process executes the next Test "
                "twice from the exact same artifact and matches Stage-0. This proves process "
                "interruption recovery for this batch adapter; it does not prove durable Forge/Environment "
                "session recovery. These focused compatibility cases do not measure Language fact reuse, "
                "mncs-test consumer is unchanged. A second workload reads the real "
                "mncs-test self-suite sources and the task module at the Compiler-pinned Language "
                "revision. One application-build/8 all-source selection resolves all seven "
                "compiler-inventoried TestResult callables from the Language inventory and emits a "
                "verified artifact; one canonical VM batch runs two rounds of all seven tests "
                "in the same admitted artifact/session. All 14 results match Stage-0 and the second "
                "round matches the first, including six passes and the typed skip result. A comment-only "
                "source shift in the real suite reuses every produced Language v3 body fact while refreshing "
                "all current Test source spans and preserving semantic Test identities and the executable "
                "artifact. This does not migrate the production runner or add Forge integration."
                " The Environment provider adapter also publishes /7 after resolving "
                "the Language-issued source Test identities. The additional cache policy check "
                "built the same source under baseline, changed capability, and changed effect-assumption "
                "requests; each variant received a distinct cache key and ran the frontend, while a "
                "fresh process reused only the exact effect-assumption request. Language verified-fact "
                "reuse is recorded by the real self-suite comment-edit workload above."
            ),
        }
        failed_boolean_checks = [
            name for name, result in report["checks"].items()
            if type(result) is bool and not result
        ]
        if failed_boolean_checks:
            raise AssertionError(
                "application-build Test inventory proof has failed checks: "
                + ", ".join(failed_boolean_checks)
            )

        evidence = ROOT / os.environ.get(
            "MNCS_TEST_INVENTORY_EVIDENCE_PATH",
            "evidence/application-build-test-inventory-20261017.json",
        )
        if evidence.exists():
            raise SystemExit(f"refusing to overwrite immutable evidence: {evidence}")
        evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
