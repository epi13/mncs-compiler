//! Direct `mncs.vm.artifact/1` emission (compiler-owned, pinned-side).
//!
//! The narrowest correct path from the backend-neutral compiler
//! boundary (elaborated `Program` -> `compile()` -> selected SSA)
//! into the canonical VM artifact, without a research-bytecode
//! payload round-trip: no `Program` dump, no interpreter-payload
//! serialization, no migration adapter on the proof path.
//!
//! The emitter runs on compiler internals (it is compiler-side), but
//! its OUTPUT is the frozen contract only: selected SSA lifted
//! verbatim, identity-bound callables, compiler-determined generic
//! entrypoints, declared capabilities, and provenance refs. The VM
//! admits the bytes without knowing compiler internals.
//!
//! Contract literals and struct shapes mirror
//! `mncs-vm/src/artifact.rs` EXACTLY (including field order: the
//! seal hashes struct-order serialization). Any drift fails loudly
//! at live admission (`IdentityMismatch`), never silently; the
//! cross-repo differential (`tools/test_vm_emit.py`) enforces it.

use std::collections::{BTreeMap, BTreeSet};

use mncs_compiler::ReferenceCompiler;
use mncs_model::{
    ArtifactRepresentation, CompilationStatus, Program, SsaModule, TargetContractRef,
};

/// Must match `mncs_vm::artifact::ARTIFACT_SCHEMA_VERSION`.
pub const VM_ARTIFACT_SCHEMA_VERSION: &str = "mncs.vm.artifact/1";
/// Must match `mncs_vm::artifact::VM_CONTRACT`.
pub const VM_CONTRACT: &str = "mncs.vm/0.1";
/// Compiler-declared lowering target for direct VM emission.
/// Promotion to the language backend registry is the follow-up if a
/// registry adapter for the VM ever lands; until then this candidate
/// is provenance carried in the artifact's target slot.
pub const VM_TARGET_CANDIDATE: &str = "mncs:target:mncs-vm-0.1";
/// Provenance backend name for directly emitted artifacts.
pub const DIRECT_BACKEND_NAME: &str = "mncs-vm-direct";

// ---------------------------------------------------------------------------
// Contract mirrors. Field order and serde attributes must match
// mncs-vm/src/artifact.rs exactly. Serialize-only: the probe never
// parses VM artifacts.
// ---------------------------------------------------------------------------

#[derive(serde::Serialize)]
struct VmArtifact {
    schema_version: String,
    #[serde(skip_serializing_if = "String::is_empty")]
    artifact_id: String,
    source: ArtifactSource,
    callables: Vec<CallableEntry>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    generic_entrypoints: Vec<GenericEntrypoint>,
    code: CodeSection,
    requirements: ArtifactRequirements,
    exports: Vec<String>,
    assumptions: Vec<String>,
    unsupported: Vec<String>,
}

#[derive(serde::Serialize)]
struct ArtifactSource {
    backend_name: String,
    backend_version: String,
    selected_ssa_identity: String,
    selected_ssa_fingerprint: String,
    target: serde_json::Value,
    lowering_refs: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    interface_identity: Option<String>,
}

#[derive(serde::Serialize)]
struct CallableEntry {
    module: String,
    name: String,
    function: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    semantic: Option<String>,
}

#[derive(serde::Serialize)]
struct GenericEntrypoint {
    generic_module: String,
    generic_function: String,
    args_spellings: Vec<String>,
    canonical_args: String,
    function: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "kebab-case")]
enum CodeSection {
    MncsSelectedSsa {
        ssa_schema: String,
        module: SsaModule,
    },
}

#[derive(serde::Serialize)]
struct BoundDecl {
    dimension: String,
    limit: u64,
}

#[derive(serde::Serialize)]
struct ArtifactRequirements {
    capabilities: Vec<String>,
    bounds: Vec<BoundDecl>,
    vm_contract: String,
}

/// `sha256:<hex>` over canonical bytes. Must match
/// `mncs_vm::artifact::artifact_id_of`.
fn artifact_id_of(bytes: &[u8]) -> String {
    format!("sha256:{}", mncs_model::sha256_hex(bytes))
}

fn vm_target_contract() -> TargetContractRef {
    TargetContractRef::new(
        VM_TARGET_CANDIDATE,
        BTreeMap::from([
            (
                "serialization".to_owned(),
                "canonical JSON artifact".to_owned(),
            ),
            (
                "integer".to_owned(),
                "MNCS SSA integer semantics".to_owned(),
            ),
            (
                "failure".to_owned(),
                "MNCS SSA explicit failure semantics".to_owned(),
            ),
            ("runtime".to_owned(), "mncs-vm reference engine".to_owned()),
            (
                "bounded-iteration".to_owned(),
                "preserve language-owned bounded iteration regions and backedges".to_owned(),
            ),
        ]),
        vec![mncs_model::SemanticId(
            "mncs:target-evidence:mncs-vm-0.1:declared-vm-contract".to_owned(),
        )],
        vec!["mncs-vm executes selected SSA under VM-owned runtime rules".to_owned()],
    )
}

/// Declared Stage-0 pin plus provisioned revision, for artifact
/// provenance. Independent of the probe cache state so emission is
/// deterministic whether the cache is on or off.
fn stage0_provenance_refs() -> Vec<String> {
    let mut refs = Vec::new();
    if let Ok(raw) = std::fs::read_to_string("mncs-language.lock.json") {
        if let Ok(lock) = serde_json::from_str::<serde_json::Value>(&raw) {
            if let Some(revision) = lock.get("revision").and_then(serde_json::Value::as_str) {
                refs.push(format!("stage0-lock:{revision}"));
            }
            if let Some(profile) = lock
                .get("source_profile")
                .and_then(serde_json::Value::as_str)
            {
                refs.push(format!("stage0-profile:{profile}"));
            }
        }
    }
    if let Ok(revision) = std::fs::read_to_string(".bootstrap/revision") {
        let revision = revision.trim();
        if !revision.is_empty() {
            refs.push(format!("stage0-provisioned:{revision}"));
        }
    }
    refs
}

/// Emit one sealed canonical artifact for an elaborated program.
///
/// Compiles to selected SSA only (no registry backend, no payload);
/// every row below mirrors the migration adapter's translation so
/// direct and migrated artifacts agree on content while only the
/// direct bytes are free of compiler internals.
pub fn emit_vm_artifact(compiler: &ReferenceCompiler, program: &Program) -> serde_json::Value {
    let emit: BTreeSet<ArtifactRepresentation> =
        [ArtifactRepresentation::Ssa].into_iter().collect();
    let request = compiler.request_for_program(program, emit, None);
    let result = compiler.compile(request, program);
    if result.status == CompilationStatus::Failed {
        panic!(
            "direct vm emission failed for {}: {:?}",
            program.module, result.diagnostics
        );
    }
    let ssa = result.emissions.ssa.clone().expect("ssa emission present");
    assert!(
        ssa.identity_is_valid(),
        "selected ssa identity is valid for {}",
        program.module
    );
    let selected = result
        .artifacts
        .iter()
        .find(|artifact| artifact.representation == ArtifactRepresentation::SelectedSsa)
        .expect("selected ssa ref present")
        .clone();

    // Exports mirror the research adapter exactly: every program
    // function name in order.
    let exports: Vec<String> = program
        .functions
        .iter()
        .map(|function| function.name.clone())
        .collect();
    let mut unsupported: Vec<String> = Vec::new();
    let mut callables: Vec<CallableEntry> = Vec::with_capacity(exports.len());
    for name in &exports {
        match resolve_export(program, &ssa, name) {
            Some(entry) => callables.push(entry),
            None => unsupported.push(format!("export {name}: no ssa instance")),
        }
    }

    let mut generic_entrypoints: Vec<GenericEntrypoint> = Vec::new();
    for row in generic_entrypoint_rows(program) {
        match resolve_generic_entry(&ssa, &row) {
            Some(entry) => generic_entrypoints.push(entry),
            None => unsupported.push(format!(
                "generic {}::{}({}): no ssa instance",
                row.0,
                row.1,
                row.2.join(", ")
            )),
        }
    }
    generic_entrypoints.sort_by(|left, right| {
        (
            &left.generic_module,
            &left.generic_function,
            &left.args_spellings,
            &left.canonical_args,
            &left.function,
        )
            .cmp(&(
                &right.generic_module,
                &right.generic_function,
                &right.args_spellings,
                &right.canonical_args,
                &right.function,
            ))
    });

    let mut capabilities: Vec<String> = ssa
        .functions
        .iter()
        .flat_map(|function| {
            function.blocks.iter().flat_map(|block| {
                block.instructions.iter().flat_map(|instruction| {
                    instruction
                        .capability_uses
                        .iter()
                        .map(|use_| use_.capability.0.clone())
                })
            })
        })
        .collect();
    capabilities.sort();
    capabilities.dedup();

    let mut lowering_refs = vec![result.identity.0.clone(), selected.identity.0.clone()];
    lowering_refs.extend(stage0_provenance_refs());

    let artifact = VmArtifact {
        schema_version: VM_ARTIFACT_SCHEMA_VERSION.to_owned(),
        artifact_id: String::new(),
        source: ArtifactSource {
            backend_name: DIRECT_BACKEND_NAME.to_owned(),
            backend_version: env!("CARGO_PKG_VERSION").to_owned(),
            selected_ssa_identity: selected.identity.0.clone(),
            selected_ssa_fingerprint: selected.fingerprint.clone(),
            target: serde_json::to_value(vm_target_contract())
                .expect("vm target contract serializes"),
            lowering_refs,
            interface_identity: Some(mncs_codegen::language_owned_interface_identity(program)),
        },
        callables,
        generic_entrypoints,
        code: CodeSection::MncsSelectedSsa {
            ssa_schema: ssa.schema_version.clone(),
            module: ssa,
        },
        requirements: ArtifactRequirements {
            capabilities,
            bounds: Vec::new(),
            vm_contract: VM_CONTRACT.to_owned(),
        },
        exports,
        assumptions: Vec::new(),
        unsupported,
    };
    let mut sealed = artifact;
    let canonical = serde_json::to_vec(&sealed).expect("artifact serializes");
    sealed.artifact_id = artifact_id_of(&canonical);
    serde_json::to_value(&sealed).expect("sealed artifact serializes")
}

/// Mirror of the migration adapter's export routing, over the typed
/// program instead of payload JSON. Panics on ambiguity (test
/// transport is loud); names without an SSA instance resolve to
/// `None` for explicit recording.
fn resolve_export(program: &Program, ssa: &SsaModule, name: &str) -> Option<CallableEntry> {
    let mut namespaces: Vec<&str> = Vec::new();
    for function in &program.functions {
        if function.name != name {
            continue;
        }
        let namespace = function.identity_namespace(&program.module);
        if !namespaces.contains(&namespace) {
            namespaces.push(namespace);
        }
    }
    let mut candidates = Vec::new();
    for namespace in &namespaces {
        let id = mncs_model::function_id(namespace, name);
        for function in &ssa.functions {
            if function.semantic_identity == id {
                candidates.push(((*namespace).to_owned(), function.identity.0.clone()));
            }
        }
    }
    match candidates.len() {
        1 => {
            let (namespace, function) = candidates.pop().unwrap_or_default();
            Some(CallableEntry {
                module: namespace,
                name: name.to_owned(),
                function,
                semantic: None,
            })
        }
        0 => None,
        _ => panic!("export {name} is ambiguous across functions"),
    }
}

/// Compiler-determined generic instantiation rows:
/// (generic_module, generic_function, spellings, canonical_args,
/// entry_module, entry_function). Replicates the backend registry's
/// row construction over public model types.
fn generic_entrypoint_rows(
    program: &Program,
) -> Vec<(String, String, Vec<String>, String, String, String)> {
    let mut rows = Vec::new();
    for record in &program.generic_specializations {
        if record.host_spellings.is_empty() {
            continue;
        }
        let generic = program.functions.iter().find(|function| {
            mncs_model::function_id(function.identity_namespace(&program.module), &function.name)
                == record.generic_function
        });
        let specialization = program.functions.iter().find(|function| {
            mncs_model::function_id(function.identity_namespace(&program.module), &function.name)
                == record.specialization_function
        });
        let (Some(generic), Some(specialization)) = (generic, specialization) else {
            continue;
        };
        for spellings in &record.host_spellings {
            rows.push((
                generic.identity_namespace(&program.module).to_owned(),
                generic.name.clone(),
                spellings.clone(),
                record.canonical_args.clone(),
                specialization
                    .identity_namespace(&program.module)
                    .to_owned(),
                specialization.name.clone(),
            ));
        }
    }
    rows
}

fn resolve_generic_entry(
    ssa: &SsaModule,
    row: &(String, String, Vec<String>, String, String, String),
) -> Option<GenericEntrypoint> {
    let id = mncs_model::function_id(&row.4, &row.5);
    let mut candidates = Vec::new();
    for function in &ssa.functions {
        if function.semantic_identity == id {
            candidates.push(function.identity.0.clone());
        }
    }
    match candidates.len() {
        1 => Some(GenericEntrypoint {
            generic_module: row.0.clone(),
            generic_function: row.1.clone(),
            args_spellings: row.2.clone(),
            canonical_args: row.3.clone(),
            function: candidates.pop().unwrap_or_default(),
        }),
        0 => None,
        _ => panic!(
            "generic {}::{} entry {}::{} is ambiguous across ssa instances",
            row.0, row.1, row.4, row.5
        ),
    }
}
