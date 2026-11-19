#!/usr/bin/env python3
"""Transport codec and canonical `mncs.vm.artifact/1` packager for the native
MNCS emitter (`mncs.compiler.vm_emit.v1`).

Responsibility boundary (kept explicit in every emitted artifact's provenance):
- Executable program lowering (functions, blocks, parameters, VM opcodes,
  resolved operand/conversion types, literal values, terminators, callable
  identities, and verification/support gating):
      owned 100% by MNCS (`mncs.compiler.vm_emit.v1` consuming native verified
      Value SSA from `mncs.compiler.ssa.v1`).
- Transport DAG encoding (`mncs-vm-artifact-codec` wire graph) and content
  SHA-256 sealing (`ssa_module_identity`, `artifact_id`):
      performed here without re-lowering or invoking Stage-0 SSA.
- Independent admission and execution:
      performed by `mncs-vm` in a fresh process.
"""

import hashlib
import json

ARTIFACT_SCHEMA_VERSION = "mncs.vm.artifact/1"
VM_CONTRACT = "mncs.vm/0.1"
SSA_SCHEMA_VERSION = "0.5"
NATIVE_EMITTER_BACKEND = "mncs-compiler-native-vm-emit"
NATIVE_EMITTER_VERSION = "0.1"
VM_TARGET_CANDIDATE = "mncs:target:mncs-vm-0.1"

_ARITH_OP_NAMES = {
    1: "add",
    2: "sub",
    3: "mul",
    4: "div",
    5: "mod",
    6: "and",
    7: "or",
    8: "xor",
    9: "shl",
    10: "shr",
}
_INTENT_NAMES = {
    1: "checked",
    2: "wrapping",
    3: "saturating",
}
_PREDICATE_NAMES = {
    1: "eq",
    2: "ne",
    3: "lt",
    4: "le",
    5: "gt",
    6: "ge",
}
_BOOL_OP_NAMES = {
    11: "and",
    12: "or",
}
_BYTE_BITWISE_NAMES = {
    6: "and",
    7: "or",
    8: "xor",
}
_BYTE_SHIFT_NAMES = {
    9: "shl",
    10: "shr",
}


def flist(node):
    """Flatten a decoded MNCS linked-list finite value."""
    out = []
    cur = node
    while isinstance(cur, dict) and "$v" in cur:
        payload = cur.get("$p") or {}
        if "head" not in payload:
            break
        out.append(payload["head"])
        cur = payload["tail"]
    return out


def _semantic_id_str(ident):
    assert ident["valid"], f"invalid SemanticIdentity: {ident}"
    return bytes(ident["bytes"][:ident["len"]]).decode("utf-8")


def _body_type(vm_ty):
    assert vm_ty["valid"], f"invalid VmScalarType: {vm_ty}"
    kind = vm_ty["kind"]
    if kind == 1:
        return "bool"
    if kind == 2:
        return "byte"
    if kind == 3:
        return {"integer": {"bits": vm_ty["bits"], "signed": vm_ty["signed"]}}
    raise ValueError(f"unsupported VmScalarType kind: {kind}")


def _integer_type(vm_ty):
    assert vm_ty["valid"] and vm_ty["kind"] == 3, f"expected integer VmScalarType: {vm_ty}"
    return {"bits": vm_ty["bits"], "signed": vm_ty["signed"]}


def _ssa_val_id(fn_ssa_id, val_id):
    return f"{fn_ssa_id}:v{val_id}"


def _ssa_blk_id(fn_ssa_id, blk_id):
    return f"{fn_ssa_id}:b{blk_id}"


def _ssa_inst_id(fn_ssa_id, blk_id, inst_id):
    return f"{fn_ssa_id}:b{blk_id}:i{inst_id}"


def _vm_inst_kind(inst):
    opcode = inst["opcode"]
    if opcode == 1:
        return {"Constant": {"value": inst["imm_u64"], "ty": _body_type(inst["ty"])}}
    if opcode == 2:
        return {"Constant": {"value": 1 if inst["imm_bool"] else 0, "ty": "bool"}}
    if opcode == 3:
        return {
            "Integer": {
                "operator": _ARITH_OP_NAMES[inst["op_code"]],
                "operand_type": _integer_type(inst["from_ty"]),
                "intent": _INTENT_NAMES[inst["intent"]],
            }
        }
    if opcode == 4:
        return {
            "IntegerCompare": {
                "predicate": _PREDICATE_NAMES[inst["op_code"]],
                "operand_type": _integer_type(inst["from_ty"]),
            }
        }
    if opcode == 5:
        return {"ByteCompare": {"predicate": _PREDICATE_NAMES[inst["op_code"]]}}
    if opcode == 6:
        return {"ByteBitwise": {"operator": _BYTE_BITWISE_NAMES[inst["op_code"]]}}
    if opcode == 7:
        return {"ByteShift": {"operator": _BYTE_SHIFT_NAMES[inst["op_code"]]}}
    if opcode == 8:
        return {"BooleanOp": {"operator": _BOOL_OP_NAMES[inst["op_code"]]}}
    if opcode == 9:
        return {"BooleanCompare": {"predicate": _PREDICATE_NAMES[inst["op_code"]]}}
    if opcode == 10:
        return "BooleanNot"
    if opcode == 11:
        return {"Convert": {"from": _body_type(inst["from_ty"]), "to": _body_type(inst["ty"])}}
    if opcode == 12:
        return {"Select": {"operand_type": _body_type(inst["ty"])}}
    if opcode == 13:
        return {
            "Call": {
                "function": _semantic_id_str(inst["callee_identity"]),
                "required_capabilities": [],
            }
        }
    raise ValueError(f"unsupported VmInstruction opcode: {opcode}")


def _vm_terminator(fn_ssa_id, term):
    disc = term["$v"]
    p = term["$p"]
    if disc in (0, "VmReturn"):
        return {"Return": {"values": [_ssa_val_id(fn_ssa_id, p["value"])]}}
    if disc in (1, "VmBranch"):
        return {
            "Branch": {
                "target": _ssa_blk_id(fn_ssa_id, p["target"]),
                "arguments": [_ssa_val_id(fn_ssa_id, item["value"]) for item in flist(p["arguments"])],
            }
        }
    if disc in (2, "VmCondBranch"):
        return {
            "ConditionalBranch": {
                "condition": _ssa_val_id(fn_ssa_id, p["condition"]),
                "then_target": _ssa_blk_id(fn_ssa_id, p["then_target"]),
                "then_arguments": [_ssa_val_id(fn_ssa_id, item["value"]) for item in flist(p["then_arguments"])],
                "else_target": _ssa_blk_id(fn_ssa_id, p["else_target"]),
                "else_arguments": [_ssa_val_id(fn_ssa_id, item["value"]) for item in flist(p["else_arguments"])],
            }
        }
    if disc in (3, "VmFailure"):
        return {"Failure": {"mode": "isolated"}}
    raise ValueError(f"unsupported VmTerminator discriminant: {disc}")


def dag_encode(value):
    """Exact mirror of `tools/vm-artifact-codec` deterministic postorder DAG encoding."""
    nodes = []
    index = {}

    def intern(key, node):
        existing = index.get(key)
        if existing is not None:
            return existing
        nid = len(nodes)
        nodes.append(node)
        index[key] = nid
        return nid

    def visit(v):
        if v is None:
            return intern(("N",), "N")
        if isinstance(v, bool):
            return intern(("B", v), {"B": v})
        if isinstance(v, int):
            return intern(("I", v), {"I": v})
        if isinstance(v, str):
            return intern(("S", v), {"S": v})
        if isinstance(v, list):
            items = tuple(visit(elem) for elem in v)
            return intern(("A", items), {"A": list(items)})
        if isinstance(v, dict):
            pairs = tuple((visit(k), visit(val)) for k, val in sorted(v.items()))
            return intern(("O", pairs), {"O": [[k, val] for k, val in pairs]})
        raise TypeError(f"unsupported JSON value for DAG codec: {type(v)}")

    root = visit(value)
    return {"nodes": nodes, "root": root}


def _sha256_hex(raw_bytes):
    return hashlib.sha256(raw_bytes).hexdigest()


def _split_function_semantic_id(sem_id):
    prefix = "mncs:0.2:function:"
    if not sem_id.startswith(prefix):
        raise ValueError(f"unexpected callable semantic identity prefix: {sem_id!r}")
    rest = sem_id[len(prefix):]
    if "::" not in rest:
        raise ValueError(f"missing '::' separator in callable semantic identity: {sem_id!r}")
    decl_module, fn_name = rest.rsplit("::", 1)
    if not decl_module or not fn_name:
        raise ValueError(f"empty declaring module or function name in semantic identity: {sem_id!r}")
    return decl_module, fn_name


def package_native_vm_artifact(
    vm_emit_result,
    source_bytes,
    *,
    allow_multi_module=False,
    closure_source_digests=None,
    producer_entry=None,
    ssa_producer_entry=None,
    emitter_source_sha256=None,
    compiler_revision=None,
    stage0_revision=None,
):
    """Package a verified MNCS `VmEmitResult` into a sealed canonical
    `mncs.vm.artifact/1` JSON dict.

    When `allow_multi_module=True`, functions from multiple declaring modules
    selected by MNCS closure lowering retain their original declaring-module
    identities in both `SsaModule.functions` and `VmArtifact.callables`.

    Raises ValueError if `vm_emit_result["ok"]` is False.
    """
    if not vm_emit_result.get("ok"):
        raise ValueError(
            f"native vm_emit rejected input: error_code={vm_emit_result.get('error_code')} "
            f"fn={vm_emit_result.get('error_function')} "
            f"blk={vm_emit_result.get('error_block')} "
            f"inst={vm_emit_result.get('error_instruction')}"
        )

    mod_start = vm_emit_result["module_name_start"]
    mod_end = vm_emit_result["module_name_end"]
    root_module_name = source_bytes[mod_start:mod_end].decode("utf-8")
    source_sha256 = _sha256_hex(source_bytes)

    ssa_functions = []
    callables = []
    exports = []
    seen_sem_ids = set()
    declaring_modules = []

    for fn in flist(vm_emit_result["functions"]):
        sem_id = _semantic_id_str(fn["identity"])
        decl_module, fn_name = _split_function_semantic_id(sem_id)
        if not allow_multi_module and decl_module != root_module_name:
            raise ValueError(
                f"unexpected callable semantic identity {sem_id!r} for single-module artifact {root_module_name!r}"
            )
        if sem_id in seen_sem_ids:
            raise ValueError(f"duplicate callable semantic identity in emitted closure: {sem_id!r}")
        seen_sem_ids.add(sem_id)
        if decl_module not in declaring_modules:
            declaring_modules.append(decl_module)
        fn_ssa_id = f"mncs:0.4:ssa:function:{decl_module}::{fn_name}"

        entry_blk_id = _ssa_blk_id(fn_ssa_id, 0)
        ssa_inputs = [
            {
                "identity": _ssa_val_id(fn_ssa_id, val["id"]),
                "semantic_identity": None,
                "ty": _body_type(val["ty"]),
                "producer": None,
                "block": entry_blk_id,
            }
            for val in flist(fn["inputs"])
        ]
        ssa_outputs = [
            {
                "identity": f"{fn_ssa_id}:result",
                "semantic_identity": None,
                "ty": _body_type(fn["result_type"]),
                "producer": None,
                "block": entry_blk_id,
            }
        ]

        ssa_blocks = []
        for blk in flist(fn["blocks"]):
            blk_id = _ssa_blk_id(fn_ssa_id, blk["id"])
            blk_params = [
                {
                    "identity": _ssa_val_id(fn_ssa_id, p["id"]),
                    "semantic_identity": None,
                    "ty": _body_type(p["ty"]),
                    "producer": None,
                    "block": blk_id,
                }
                for p in flist(blk["parameters"])
            ]
            ssa_insts = []
            for inst in flist(blk["instructions"]):
                inst_id = _ssa_inst_id(fn_ssa_id, blk["id"], inst["id"])
                ssa_insts.append({
                    "identity": inst_id,
                    "semantic_identity": None,
                    "hir_identity": None,
                    "kind": _vm_inst_kind(inst),
                    "inputs": [_ssa_val_id(fn_ssa_id, item["value"]) for item in flist(inst["inputs"])],
                    "outputs": [
                        {
                            "identity": _ssa_val_id(fn_ssa_id, inst["result"]),
                            "semantic_identity": None,
                            "ty": _body_type(inst["ty"]),
                            "producer": inst_id,
                            "block": blk_id,
                        }
                    ],
                    "effects": [],
                    "capability_uses": [],
                    "contracts": [],
                    "assumptions": [],
                    "evidence": [],
                    "machine_intent": None,
                    "obligations": [],
                    "failure": None,
                })

            ssa_blocks.append({
                "identity": blk_id,
                "semantic_identity": None,
                "path": "error" if blk["is_error_path"] else "normal",
                "parameters": blk_params,
                "instructions": ssa_insts,
                "terminator": _vm_terminator(fn_ssa_id, blk["terminator"]),
                "failure_target": (
                    _ssa_blk_id(fn_ssa_id, blk["failure_target"])
                    if blk["has_failure_target"]
                    else None
                ),
            })

        ssa_functions.append({
            "identity": fn_ssa_id,
            "semantic_identity": sem_id,
            "inputs": ssa_inputs,
            "outputs": ssa_outputs,
            "blocks": ssa_blocks,
            "failure": "isolated",
        })
        callables.append({
            "module": decl_module,
            "name": fn_name,
            "function": fn_ssa_id,
            "semantic": sem_id,
        })
        exports.append(fn_ssa_id)

    module_sem_id = f"mncs:0.2:module:{root_module_name}"
    if closure_source_digests:
        closure_digest = _sha256_hex(
            json.dumps(list(closure_source_digests), separators=(",", ":")).encode("utf-8")
        )
        hir_fingerprint = f"sha256:{closure_digest}"
    else:
        hir_fingerprint = f"sha256:{source_sha256}"
    ssa_module_id = f"mncs:0.4:ssa:module:{_sha256_hex(f'{module_sem_id}:{hir_fingerprint}'.encode())}"

    ssa_module = {
        "schema_version": SSA_SCHEMA_VERSION,
        "identity": ssa_module_id,
        "semantic_identity": module_sem_id,
        "hir_fingerprint": hir_fingerprint,
        "functions": ssa_functions,
        "obligations": [],
        "trace": {"entries": []},
        "transformations": [],
    }
    ssa_fingerprint = _sha256_hex(
        json.dumps(ssa_module, separators=(",", ":")).encode("utf-8")
    )
    dag_module = dag_encode(ssa_module)

    effective_producer = producer_entry or (
        "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module"
        if allow_multi_module
        else "mncs.compiler.vm_emit.v1::compile_and_emit_vm_module"
    )
    effective_ssa_producer = ssa_producer_entry or (
        "mncs.compiler.project.v1::compile_project"
        if allow_multi_module
        else "mncs.compiler.ssa.v1::lower_value_ssa"
    )
    lowering_refs = [
        f"executable-program-producer:{effective_producer}",
        f"native-ssa-producer:{effective_ssa_producer}",
        "transport-codec:tools/native_vm_artifact.py",
        "independent-admission:mncs-vm",
        f"root-source-sha256:{source_sha256}",
    ]
    if allow_multi_module:
        lowering_refs.append(f"declaring-modules:{','.join(declaring_modules)}")
    if closure_source_digests:
        for src_id, src_sha in closure_source_digests:
            lowering_refs.append(f"closure-source:{src_id}=sha256:{src_sha}")
    if emitter_source_sha256:
        lowering_refs.append(f"emitter-source-sha256:{emitter_source_sha256}")
    if compiler_revision:
        lowering_refs.append(f"compiler-revision:{compiler_revision}")
    if stage0_revision:
        lowering_refs.append(f"stage0-host-execution-lock:{stage0_revision}")

    # Keys inside `target` must be sorted because Rust deserializes `target`
    # into `serde_json::Value` (BTreeMap) before recomputing `canonical_bytes()`.
    target_meta = {
        "assumptions": {
            "bounded-iteration": "not admitted in bounded scalar subset",
            "failure": "MNCS SSA explicit failure semantics",
            "integer": "MNCS SSA integer semantics",
            "runtime": "mncs-vm reference engine",
            "serialization": "canonical JSON artifact",
        },
        "evidence": ["mncs:target-evidence:mncs-vm-0.1:native-mncs-vm-emit"],
        "notes": [
            "Executable SSA emitted by MNCS module mncs.compiler.vm_emit.v1 from native verified Value SSA",
            "DAG transport encoding and SHA-256 sealing handled by tools/native_vm_artifact.py",
            "Independent admission and execution handled by mncs-vm",
        ],
        "target": VM_TARGET_CANDIDATE,
    }

    # Exact field order of `mncs_vm::artifact::VmArtifact::canonical_bytes()` Content struct!
    content_for_seal = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "source": {
            "backend_name": NATIVE_EMITTER_BACKEND,
            "backend_version": NATIVE_EMITTER_VERSION,
            "selected_ssa_identity": ssa_module_id,
            "selected_ssa_fingerprint": ssa_fingerprint,
            "target": target_meta,
            "lowering_refs": lowering_refs,
        },
        "callables": callables,
        "code": {
            "mncs-selected-ssa": {
                "ssa_schema": SSA_SCHEMA_VERSION,
                "module": dag_module,
            }
        },
        "requirements": {
            "capabilities": [],
            "bounds": [],
            "vm_contract": VM_CONTRACT,
        },
        "exports": exports,
        "assumptions": [
            "bounded pure scalar and direct-call subset emitted from native verified Value SSA by mncs.compiler.vm_emit.v1",
        ],
        "unsupported": [],
    }
    canonical_bytes = json.dumps(content_for_seal, separators=(",", ":")).encode("utf-8")
    artifact_id = f"sha256:{_sha256_hex(canonical_bytes)}"

    # Exact field order of `mncs_vm::artifact::VmArtifact` with `artifact_id` inserted after `schema_version`.
    sealed = {
        "schema_version": content_for_seal["schema_version"],
        "artifact_id": artifact_id,
        "source": content_for_seal["source"],
        "callables": content_for_seal["callables"],
        "code": content_for_seal["code"],
        "requirements": content_for_seal["requirements"],
        "exports": content_for_seal["exports"],
        "assumptions": content_for_seal["assumptions"],
        "unsupported": content_for_seal["unsupported"],
    }
    return sealed
