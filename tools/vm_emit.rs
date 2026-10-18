//! Direct `mncs.vm.artifact/1` emission (compiler-owned, pinned-side).
//!
//! The narrowest correct path from the backend-neutral compiler
//! boundary (elaborated `Program` -> `compile()` -> selected SSA)
//! into the canonical VM artifact, without a research-bytecode
//! payload round-trip: no `Program` dump, no interpreter-payload
//! serialization, no migration adapter on the proof path.
//!
//! The emitter runs on compiler internals (it is compiler-side), but
//! its OUTPUT is the frozen contract only: selected SSA encoded losslessly, identity-bound callables, compiler-determined generic
//! entrypoints, declared capabilities, and provenance refs. The VM
//! admits the bytes without knowing compiler internals.
//!
//! Contract literals and struct shapes mirror
//! `mncs-vm/src/artifact.rs` EXACTLY (including field order: the
//! seal hashes struct-order serialization). Any drift fails loudly
//! at live admission (`IdentityMismatch`), never silently; the
//! cross-repo differential (`tools/test_vm_emit.py`) enforces it.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use mncs_compiler::ReferenceCompiler;
use mncs_model::{
    ArtifactRepresentation, BodyOperationKind, BodyType, BoundsEvidence, CompilationStatus,
    Program, SequenceBound, SsaInstruction, SsaInstructionKind, SsaModule, SsaValue,
    TargetContractRef, TargetIdentity,
};

const VM_STATIC_SEQUENCE_COPY_UNROLL_LIMIT: u32 = 256;
const VM_STATIC_SEQUENCE_COPY_REBUILD_BOUND_LIMIT: u32 = 512;

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
    #[serde(skip_serializing_if = "Option::is_none")]
    test_binding: Option<mncs_model::BackendCallableBinding>,
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
        // The shared codec freezes this once before sealing. VM decoding
        // expands it into its typed SSA module without a JSON tree copy.
        module: serde_json::Value,
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

/// One compiler-verified mapping from a canonical capability identity to
/// the exact key used by the selected VM instruction.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
pub struct CapabilityDispatchBinding {
    pub capability_identity: String,
    pub runtime_capability_key: String,
    pub runtime_key_kind: String,
    pub operation: String,
    pub function_identity: String,
    pub instruction_identity: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_operation_identity: Option<String>,
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
pub fn emit_vm_artifact(
    compiler: &ReferenceCompiler,
    program: &Program,
) -> Result<serde_json::Value, String> {
    emit_vm_artifact_with_producer(compiler, program, None)
}

pub fn emit_vm_artifact_with_producer(
    compiler: &ReferenceCompiler,
    program: &Program,
    producer: Option<&str>,
) -> Result<serde_json::Value, String> {
    emit_vm_artifact_with_source_map(compiler, program, producer, None).map(|pair| pair.0)
}

pub fn emit_vm_artifact_with_source_map(
    compiler: &ReferenceCompiler,
    program: &Program,
    producer: Option<&str>,
    source_map: Option<mncs_compiler::ExecutionSourceMap>,
) -> Result<(serde_json::Value, Option<mncs_compiler::ExecutionSourceMap>), String> {
    emit_vm_artifact_scoped(compiler, program, producer, source_map, None, None)
        .map(|(artifact, source_map, _, _)| (artifact, source_map))
}

/// Emit a closed application surface consisting of its selected entry and
/// concrete generic specializations explicitly requested by the build.
/// Unselected, uninstantiated declarations are returned separately so they
/// cannot make this entry artifact appear complete by accident.
pub fn emit_vm_application_artifact_with_producer(
    compiler: &ReferenceCompiler,
    program: &Program,
    producer: Option<&str>,
    entry_module: &str,
    entry_function: &str,
    generic_seed_functions: &BTreeSet<(String, String)>,
) -> Result<
    (
        serde_json::Value,
        Vec<String>,
        Vec<CapabilityDispatchBinding>,
    ),
    String,
> {
    let callable_roots = BTreeSet::from([(entry_module.to_owned(), entry_function.to_owned())]);
    emit_vm_application_artifact_for_callables_with_producer(
        compiler,
        program,
        producer,
        &callable_roots,
        generic_seed_functions,
    )
}

/// Emit one canonical VM artifact for a selected set of non-generic
/// callables plus any explicitly requested generic specializations.
pub fn emit_vm_application_artifact_for_callables_with_producer(
    compiler: &ReferenceCompiler,
    program: &Program,
    producer: Option<&str>,
    callable_roots: &BTreeSet<(String, String)>,
    generic_seed_functions: &BTreeSet<(String, String)>,
) -> Result<
    (
        serde_json::Value,
        Vec<String>,
        Vec<CapabilityDispatchBinding>,
    ),
    String,
> {
    if callable_roots.is_empty() {
        return Err("application callable set has no selected roots".to_owned());
    }
    let mut selected_exports = callable_roots.clone();
    for (generic_module, generic_function) in generic_seed_functions {
        let generic_identity = mncs_model::function_id(generic_module, generic_function);
        for record in &program.generic_specializations {
            if record.generic_function != generic_identity {
                continue;
            }
            if let Some(function) = program.functions.iter().find(|function| {
                mncs_model::function_id(
                    function.identity_namespace(&program.module),
                    &function.name,
                ) == record.specialization_function
            }) {
                selected_exports.insert((
                    function.identity_namespace(&program.module).to_owned(),
                    function.name.clone(),
                ));
            }
        }
    }
    let (artifact, _, unselected_unresolved, capability_bindings) = emit_vm_artifact_scoped(
        compiler,
        program,
        producer,
        None,
        Some(&selected_exports),
        Some(generic_seed_functions),
    )?;
    Ok((artifact, unselected_unresolved, capability_bindings))
}

fn collect_record_type_ids(ty: &BodyType, identities: &mut BTreeSet<String>) {
    match ty {
        BodyType::Record { identity, .. } => {
            identities.insert(identity.0.clone());
        }
        BodyType::Sequence { element, .. } | BodyType::Vector { element, .. } => {
            collect_record_type_ids(element, identities);
        }
        BodyType::Named(_)
        | BodyType::Bool
        | BodyType::Integer(_)
        | BodyType::Float(_)
        | BodyType::Byte
        | BodyType::Mask { .. }
        | BodyType::Finite { .. }
        | BodyType::GenericParam { .. } => {}
    }
}

fn collect_instruction_record_type_ids(
    kind: &SsaInstructionKind,
    identities: &mut BTreeSet<String>,
) {
    match kind {
        SsaInstructionKind::Constant { ty, .. }
        | SsaInstructionKind::Select { operand_type: ty } => {
            collect_record_type_ids(ty, identities);
        }
        SsaInstructionKind::SequenceReplace { element_type, .. }
        | SsaInstructionKind::SequenceCopy { element_type, .. }
        | SsaInstructionKind::VectorConstruct { element_type, .. }
        | SsaInstructionKind::VectorSplat { element_type, .. }
        | SsaInstructionKind::VectorExtract { element_type, .. }
        | SsaInstructionKind::VectorReplace { element_type, .. }
        | SsaInstructionKind::VectorBinary { element_type, .. }
        | SsaInstructionKind::VectorCompare { element_type, .. }
        | SsaInstructionKind::VectorReduce { element_type, .. } => {
            collect_record_type_ids(element_type, identities);
        }
        SsaInstructionKind::Convert { from, to } => {
            collect_record_type_ids(from, identities);
            collect_record_type_ids(to, identities);
        }
        SsaInstructionKind::SequenceConstruct { element_type, .. } => {
            collect_record_type_ids(element_type, identities);
        }
        SsaInstructionKind::RecordConstruct { type_identity, .. }
        | SsaInstructionKind::RecordProject { type_identity, .. } => {
            identities.insert(type_identity.0.clone());
        }
        SsaInstructionKind::Integer { .. }
        | SsaInstructionKind::IntegerCompare { .. }
        | SsaInstructionKind::FloatConstant { .. }
        | SsaInstructionKind::Float { .. }
        | SsaInstructionKind::FloatCompare { .. }
        | SsaInstructionKind::FloatIntrinsic { .. }
        | SsaInstructionKind::BooleanOp { .. }
        | SsaInstructionKind::BooleanCompare { .. }
        | SsaInstructionKind::BooleanNot
        | SsaInstructionKind::ByteBitwise { .. }
        | SsaInstructionKind::ByteShift { .. }
        | SsaInstructionKind::ByteCompare { .. }
        | SsaInstructionKind::BoundCheck { .. }
        | SsaInstructionKind::MaskBinary { .. }
        | SsaInstructionKind::MaskNot { .. }
        | SsaInstructionKind::MaskReduce { .. }
        | SsaInstructionKind::SequenceProject { .. }
        | SsaInstructionKind::SequenceLength { .. }
        | SsaInstructionKind::ViewConstruct { .. }
        | SsaInstructionKind::ViewNarrow { .. }
        | SsaInstructionKind::FiniteConstruct { .. }
        | SsaInstructionKind::FinitePayloadProject { .. }
        | SsaInstructionKind::FiniteIsVariant { .. }
        | SsaInstructionKind::Call { .. }
        | SsaInstructionKind::Effect
        | SsaInstructionKind::HostCall { .. }
        | SsaInstructionKind::RuntimeCheck { .. } => {}
    }
}

fn retain_application_ssa_record_type_closure(
    ssa: &mut SsaModule,
    mut pending: VecDeque<String>,
) -> Result<(), String> {
    let mut record_indices = BTreeMap::<String, usize>::new();
    for (index, record) in ssa.record_types.iter().enumerate() {
        if record_indices
            .insert(record.identity.0.clone(), index)
            .is_some()
        {
            return Err(format!(
                "duplicate application SSA record type identity {}",
                record.identity.0
            ));
        }
    }

    let mut reachable = BTreeSet::<String>::new();
    while let Some(identity) = pending.pop_front() {
        if !reachable.insert(identity.clone()) {
            continue;
        }
        let Some(index) = record_indices.get(&identity).copied() else {
            return Err(format!(
                "selected application SSA references missing record type {identity}"
            ));
        };
        let record = &ssa.record_types[index];
        let mut nested = BTreeSet::new();
        for (_, field_type) in &record.fields {
            collect_record_type_ids(field_type, &mut nested);
        }
        pending.extend(nested);
    }

    ssa.record_types
        .retain(|record| reachable.contains(&record.identity.0));
    Ok(())
}

/// Project a linked program's verified SSA to the selected application roots
/// and their transitive compiler-resolved call closure. The originating full
/// SSA remains the provenance authority; this closed module is revalidated
/// before it is serialized into an application artifact.
fn application_ssa_closure(
    mut ssa: SsaModule,
    program: &Program,
    selected_exports: &BTreeSet<(String, String)>,
) -> Result<SsaModule, String> {
    if selected_exports.is_empty() {
        return Err("application SSA closure has no selected roots".to_owned());
    }
    if !ssa.identity_is_valid() {
        return Err("application source SSA identity is invalid".to_owned());
    }
    if !ssa.proof_relationships.is_empty() {
        return Err(
            "application SSA closure cannot project attached proof relationships".to_owned(),
        );
    }

    let mut functions_by_identity = BTreeMap::<String, usize>::new();
    for (index, function) in ssa.functions.iter().enumerate() {
        for identity in [&function.semantic_identity.0, &function.identity.0] {
            if let Some(previous) = functions_by_identity.insert(identity.clone(), index) {
                if previous != index {
                    return Err(format!(
                        "duplicate application SSA function identity {identity}"
                    ));
                }
            }
        }
    }

    let mut pending = VecDeque::<String>::new();
    for (module, function) in selected_exports {
        pending.push_back(mncs_model::function_id(module, function).0);
    }
    let mut reachable = BTreeSet::<String>::new();
    while let Some(identity) = pending.pop_front() {
        let Some(index) = functions_by_identity.get(&identity).copied() else {
            return Err(format!(
                "selected application call target {identity} has no verified SSA function"
            ));
        };
        let function = &ssa.functions[index];
        let semantic_identity = function.semantic_identity.0.clone();
        if !reachable.insert(semantic_identity.clone()) {
            continue;
        }
        if function.blocks.is_empty() {
            return Err(format!(
                "selected application call target {semantic_identity} has no executable body"
            ));
        }
        for block in &function.blocks {
            for instruction in &block.instructions {
                if let SsaInstructionKind::Call { function, .. } = &instruction.kind {
                    pending.push_back(function.0.clone());
                }
            }
        }
        for iteration in &function.bounded_iterations {
            pending.extend(iteration.callees.iter().map(|callee| callee.0.clone()));
        }
    }

    ssa.functions
        .retain(|function| reachable.contains(&function.semantic_identity.0));
    if ssa.functions.len() != reachable.len() {
        return Err("application SSA closure did not resolve every selected function".to_owned());
    }

    let mut required_record_types = BTreeSet::<String>::new();
    for function in &ssa.functions {
        for value in function.inputs.iter().chain(&function.outputs) {
            collect_record_type_ids(&value.ty, &mut required_record_types);
        }
        for iteration in &function.bounded_iterations {
            collect_record_type_ids(&iteration.state_type, &mut required_record_types);
            if let mncs_model::IterationDomain::OverSequence { element_type } = &iteration.domain {
                collect_record_type_ids(element_type, &mut required_record_types);
            }
        }
        for block in &function.blocks {
            for value in &block.parameters {
                collect_record_type_ids(&value.ty, &mut required_record_types);
            }
            for instruction in &block.instructions {
                collect_instruction_record_type_ids(&instruction.kind, &mut required_record_types);
                for value in &instruction.outputs {
                    collect_record_type_ids(&value.ty, &mut required_record_types);
                }
            }
        }
    }
    retain_application_ssa_record_type_closure(
        &mut ssa,
        required_record_types.into_iter().collect(),
    )?;

    // The executable closure must not carry the source program's unrelated
    // semantic index and lowering history. ArtifactSource binds the original
    // selected-SSA fingerprint; this module retains only source correlation,
    // obligations, and specializations attached to executable functions.
    let mut reachable_ssa_ids = BTreeSet::<String>::new();
    let mut reachable_semantic_ids = BTreeSet::<String>::new();
    let mut reachable_hir_ids = BTreeSet::<String>::new();
    let mut obligation_ids = BTreeSet::<String>::new();
    for function in &ssa.functions {
        reachable_ssa_ids.insert(function.identity.0.clone());
        reachable_semantic_ids.insert(function.semantic_identity.0.clone());
        for value in function.inputs.iter().chain(&function.outputs) {
            reachable_ssa_ids.insert(value.identity.0.clone());
            if let Some(identity) = &value.semantic_identity {
                reachable_semantic_ids.insert(identity.0.clone());
            }
        }
        for iteration in &function.bounded_iterations {
            reachable_ssa_ids.insert(iteration.identity.0.clone());
            reachable_ssa_ids.insert(iteration.preheader.0.clone());
            reachable_ssa_ids.insert(iteration.header.0.clone());
            reachable_ssa_ids.insert(iteration.body_entry.0.clone());
            reachable_ssa_ids.insert(iteration.backedge.0.clone());
            reachable_ssa_ids.insert(iteration.exit.0.clone());
            reachable_ssa_ids.extend(iteration.body_blocks.iter().map(|id| id.0.clone()));
            obligation_ids.extend(iteration.obligations.iter().map(|id| id.0.clone()));
        }
        for block in &function.blocks {
            reachable_ssa_ids.insert(block.identity.0.clone());
            if let Some(identity) = &block.semantic_identity {
                reachable_semantic_ids.insert(identity.0.clone());
            }
            for value in &block.parameters {
                reachable_ssa_ids.insert(value.identity.0.clone());
                if let Some(identity) = &value.semantic_identity {
                    reachable_semantic_ids.insert(identity.0.clone());
                }
            }
            for instruction in &block.instructions {
                reachable_ssa_ids.insert(instruction.identity.0.clone());
                if let Some(identity) = &instruction.semantic_identity {
                    reachable_semantic_ids.insert(identity.0.clone());
                }
                if let Some(identity) = &instruction.hir_identity {
                    reachable_hir_ids.insert(identity.0.clone());
                }
                obligation_ids.extend(instruction.obligations.iter().map(|id| id.0.clone()));
                for value in &instruction.outputs {
                    reachable_ssa_ids.insert(value.identity.0.clone());
                    if let Some(identity) = &value.semantic_identity {
                        reachable_semantic_ids.insert(identity.0.clone());
                    }
                }
            }
        }
    }

    // Preserve the obligation dependency closure for every retained
    // instruction and bounded iteration, while excluding unrelated source
    // obligations. Dependencies that are not obligations remain provenance
    // references and do not cause unrelated obligation records to be copied.
    let obligations_by_id: BTreeMap<_, _> = ssa
        .obligations
        .iter()
        .map(|obligation| (obligation.identity.0.clone(), obligation))
        .collect();
    let mut pending_obligations: Vec<_> = obligation_ids.iter().cloned().collect();
    while let Some(identity) = pending_obligations.pop() {
        let Some(obligation) = obligations_by_id.get(&identity) else {
            continue;
        };
        for dependency in obligation
            .dependencies
            .iter()
            .chain(&obligation.assumptions)
        {
            if obligations_by_id.contains_key(&dependency.0)
                && obligation_ids.insert(dependency.0.clone())
            {
                pending_obligations.push(dependency.0.clone());
            }
        }
    }
    drop(obligations_by_id);
    ssa.obligations
        .retain(|obligation| obligation_ids.contains(&obligation.identity.0));
    ssa.trace.entries.retain_mut(|entry| {
        let semantic_is_reachable = reachable_semantic_ids.contains(&entry.semantic_identity.0);
        entry
            .ssa_identities
            .retain(|identity| reachable_ssa_ids.contains(&identity.0));
        entry
            .hir_identities
            .retain(|identity| reachable_hir_ids.contains(&identity.0));
        semantic_is_reachable
            || !entry.ssa_identities.is_empty()
            || !entry.hir_identities.is_empty()
    });
    ssa.transformations.retain(|transformation| {
        reachable_semantic_ids.contains(&transformation.operation.0)
            || reachable_ssa_ids.contains(&transformation.operation.0)
            || reachable_ssa_ids.contains(&transformation.output_identity.0)
    });
    ssa.generic_specializations
        .retain(|specialization| reachable.contains(&specialization.specialization_function.0));
    // The VM executor does not consult the whole-program binding index; its
    // full content remains bound by ArtifactSource's original SSA fingerprint.
    ssa.binding_table = None;

    let validation = ssa.validate_lowering_boundary(program);
    if !validation.valid {
        return Err(format!(
            "projected application SSA failed backend validation: {:?}",
            validation.errors
        ));
    }
    if !ssa.identity_is_valid() {
        return Err("projected application SSA identity is invalid".to_owned());
    }
    Ok(ssa)
}

/// Construct a backend input whose executable bodies are limited to the
/// selected application's verified call closure. The caller first validates
/// the complete source program; declaration interfaces and all provenance
/// tables stay present so backend identities and call resolution remain bound
/// to the complete linked application.
fn application_lowering_program(
    program: &Program,
    selected_exports: &BTreeSet<(String, String)>,
) -> Result<Program, String> {
    if selected_exports.is_empty() {
        return Err("application lowering has no selected roots".to_owned());
    }

    let validation = program.validate();
    if !validation.valid {
        return Err(format!(
            "application source program failed semantic validation: {:?}",
            validation.errors
        ));
    }

    let functions_by_identity: BTreeMap<_, _> = program
        .functions
        .iter()
        .map(|function| {
            (
                mncs_model::function_id(
                    function.identity_namespace(&program.module),
                    &function.name,
                )
                .0,
                function,
            )
        })
        .collect();
    let mut pending: VecDeque<String> = selected_exports
        .iter()
        .map(|(module, function)| mncs_model::function_id(module, function).0)
        .collect();
    let mut reachable = BTreeSet::new();
    while let Some(identity) = pending.pop_front() {
        if !reachable.insert(identity.clone()) {
            continue;
        }
        let function = functions_by_identity.get(&identity).ok_or_else(|| {
            format!("selected application function {identity} has no source declaration")
        })?;
        if !function.generic_params.is_empty() {
            return Err(format!(
                "selected application function {identity} is an unresolved generic template"
            ));
        }
        let body = function.body.as_ref().ok_or_else(|| {
            format!("selected application function {identity} has no executable body")
        })?;
        for block in &body.blocks {
            for operation in &block.operations {
                if let BodyOperationKind::Call {
                    function: callee,
                    specialization,
                    ..
                } = &operation.kind
                {
                    if specialization
                        .as_ref()
                        .is_some_and(|specialization| specialization != callee)
                    {
                        return Err(format!(
                            "application call in {identity} has mismatched generic specialization identity"
                        ));
                    }
                    pending.push_back(callee.0.clone());
                }
            }
        }
        for iteration in &body.bounded_iterations {
            pending.extend(iteration.callees.iter().map(|callee| callee.0.clone()));
        }
    }

    let mut lowering_program = program.clone();
    for function in &mut lowering_program.functions {
        let identity = mncs_model::function_id(
            function.identity_namespace(&lowering_program.module),
            &function.name,
        );
        if !reachable.contains(&identity.0) {
            function.body = None;
        }
    }
    Ok(lowering_program)
}

/// Lower statically proven exact-bound span copies into VM-supported
/// projections, replacements, or bounded reconstruction. The canonical VM
/// deliberately refuses the `SequenceCopy` instruction today; for literal
/// windows, the compiler can express the same total operation using its
/// existing sequence subset without adding runtime checks or changing the
/// source-level proof. Dynamic windows and copies whose destination is too
/// large to reconstruct remain untouched and retain the VM's explicit
/// unsupported outcome.
fn lower_vm_static_sequence_copies(ssa: &mut SsaModule) -> Result<usize, String> {
    let target = TargetIdentity {
        identity: mncs_model::SemanticId("mncs:0.2:target:mncs-vm-0.1".to_owned()),
        family: "mncs-vm".to_owned(),
        features: vec![
            "sequence-construct".to_owned(),
            "sequence-project".to_owned(),
            "sequence-replace".to_owned(),
        ],
        integer_widths: vec![64],
        abi: None,
    };
    let implementation =
        mncs_model::SemanticId("mncs:0.2:compiler:vm-static-exact-sequence-copy/2".to_owned());
    let mut lowered_count = 0usize;
    let mut transformations = Vec::new();

    for function in &mut ssa.functions {
        let mut constants = BTreeMap::new();
        let mut types = BTreeMap::new();
        for value in &function.inputs {
            types.insert(value.identity.clone(), value.ty.clone());
        }
        for value in &function.outputs {
            types.insert(value.identity.clone(), value.ty.clone());
        }
        for block in &function.blocks {
            for value in &block.parameters {
                types.insert(value.identity.clone(), value.ty.clone());
            }
            for instruction in &block.instructions {
                for value in &instruction.outputs {
                    types.insert(value.identity.clone(), value.ty.clone());
                }
                if let (SsaInstructionKind::Constant { value, ty }, Some(output)) =
                    (&instruction.kind, instruction.outputs.first())
                {
                    if let BodyType::Integer(integer) = ty {
                        if *integer
                            == (mncs_model::IntegerType {
                                bits: 64,
                                signed: false,
                            })
                        {
                            constants.insert(output.identity.clone(), *value);
                        }
                    }
                }
            }
        }

        for block in &mut function.blocks {
            let mut rewritten = Vec::with_capacity(block.instructions.len());
            for instruction in std::mem::take(&mut block.instructions) {
                let SsaInstructionKind::SequenceCopy {
                    element_type,
                    dst_bound,
                    src_bound,
                    evidence: BoundsEvidence::StaticExact,
                } = &instruction.kind
                else {
                    rewritten.push(instruction);
                    continue;
                };
                let (SequenceBound::Exact(dst_length), SequenceBound::Exact(src_length)) =
                    (dst_bound, src_bound)
                else {
                    rewritten.push(instruction);
                    continue;
                };
                if instruction.inputs.len() != 5 || instruction.outputs.len() != 1 {
                    return Err(format!(
                        "static SequenceCopy {} has an invalid operand or result count",
                        instruction.identity.0
                    ));
                }
                let (Some(dst_at), Some(src_at), Some(length)) = (
                    constants.get(&instruction.inputs[1]),
                    constants.get(&instruction.inputs[3]),
                    constants.get(&instruction.inputs[4]),
                ) else {
                    rewritten.push(instruction);
                    continue;
                };
                if *dst_at < 0 || *src_at < 0 || *length < 0 {
                    return Err(format!(
                        "static SequenceCopy {} carries a negative exact window",
                        instruction.identity.0
                    ));
                }
                let (dst_at, src_at, length) = (*dst_at as u128, *src_at as u128, *length as u128);
                if dst_at + length > u128::from(*dst_length)
                    || src_at + length > u128::from(*src_length)
                {
                    return Err(format!(
                        "static SequenceCopy {} violates its StaticExact bounds evidence",
                        instruction.identity.0
                    ));
                }
                let reconstruct_bounded_destination = length
                    > u128::from(VM_STATIC_SEQUENCE_COPY_UNROLL_LIMIT)
                    && *dst_length <= VM_STATIC_SEQUENCE_COPY_REBUILD_BOUND_LIMIT;
                if length > u128::from(VM_STATIC_SEQUENCE_COPY_UNROLL_LIMIT)
                    && !reconstruct_bounded_destination
                {
                    rewritten.push(instruction);
                    continue;
                }

                let destination_type = types.get(&instruction.inputs[0]).ok_or_else(|| {
                    format!(
                        "SequenceCopy {} destination type is missing",
                        instruction.identity.0
                    )
                })?;
                let source_type = types.get(&instruction.inputs[2]).ok_or_else(|| {
                    format!(
                        "SequenceCopy {} source type is missing",
                        instruction.identity.0
                    )
                })?;
                let expected_type = BodyType::Sequence {
                    element: Box::new(element_type.clone()),
                    bound: dst_bound.clone(),
                };
                if destination_type != &expected_type
                    || source_type
                        != &(BodyType::Sequence {
                            element: Box::new(element_type.clone()),
                            bound: src_bound.clone(),
                        })
                    || instruction.outputs[0].ty != expected_type
                {
                    return Err(format!(
                        "static SequenceCopy {} types disagree with its exact bounds",
                        instruction.identity.0
                    ));
                }

                let owner = instruction.identity.clone();
                let block_identity = block.identity.clone();
                let mut ordinal = 0u64;
                if length == 0 {
                    let condition_id = vm_sequence_copy_id(&owner, "empty-condition", ordinal);
                    ordinal += 1;
                    let condition = SsaValue {
                        identity: vm_sequence_copy_id(&owner, "empty-condition-value", ordinal),
                        semantic_identity: None,
                        ty: BodyType::Bool,
                        producer: Some(condition_id.clone()),
                        block: block_identity.clone(),
                    };
                    let condition_value = condition.identity.clone();
                    rewritten.push(vm_sequence_copy_instruction(
                        condition_id,
                        SsaInstructionKind::Constant {
                            value: 1,
                            ty: BodyType::Bool,
                        },
                        Vec::new(),
                        vec![condition],
                    ));
                    let output_id = vm_sequence_copy_id(&owner, "empty-result", ordinal);
                    let mut output = instruction.outputs[0].clone();
                    output.producer = Some(output_id.clone());
                    let mut lowered = instruction.clone();
                    lowered.identity = output_id.clone();
                    lowered.kind = SsaInstructionKind::Select {
                        operand_type: expected_type.clone(),
                    };
                    lowered.inputs = vec![
                        condition_value,
                        instruction.inputs[0].clone(),
                        instruction.inputs[0].clone(),
                    ];
                    lowered.outputs = vec![output];
                    rewritten.push(lowered);
                    record_vm_sequence_copy_lowering(
                        &mut transformations,
                        &instruction,
                        output_id,
                        &target,
                        &implementation,
                        "vm-static-exact-sequence-copy-to-project-replace/1",
                    );
                    lowered_count += 1;
                    continue;
                }

                if reconstruct_bounded_destination {
                    let mut projected_elements = Vec::with_capacity(*dst_length as usize);
                    let copied_end = dst_at + length;
                    for destination_offset in 0..*dst_length {
                        let destination_offset = u128::from(destination_offset);
                        let (sequence, bound, source_offset) =
                            if destination_offset >= dst_at && destination_offset < copied_end {
                                (
                                    instruction.inputs[2].clone(),
                                    src_bound.clone(),
                                    src_at + (destination_offset - dst_at),
                                )
                            } else {
                                (
                                    instruction.inputs[0].clone(),
                                    dst_bound.clone(),
                                    destination_offset,
                                )
                            };
                        let index = push_vm_sequence_copy_constant(
                            &mut rewritten,
                            &owner,
                            &mut ordinal,
                            &block_identity,
                            u64::try_from(source_offset)
                                .expect("validated exact sequence index fits u64"),
                        );
                        let project_id = vm_sequence_copy_id(&owner, "rebuild-project", ordinal);
                        ordinal += 1;
                        let projected = SsaValue {
                            identity: vm_sequence_copy_id(&owner, "rebuild-project-value", ordinal),
                            semantic_identity: None,
                            ty: element_type.clone(),
                            producer: Some(project_id.clone()),
                            block: block_identity.clone(),
                        };
                        projected_elements.push(projected.identity.clone());
                        rewritten.push(vm_sequence_copy_instruction(
                            project_id,
                            SsaInstructionKind::SequenceProject {
                                bound,
                                evidence: BoundsEvidence::StaticExact,
                            },
                            vec![sequence, index],
                            vec![projected],
                        ));
                    }

                    let construct_id = vm_sequence_copy_id(&owner, "rebuild-result", ordinal);
                    let mut output = instruction.outputs[0].clone();
                    output.producer = Some(construct_id.clone());
                    let mut construct = instruction.clone();
                    construct.identity = construct_id.clone();
                    construct.kind = SsaInstructionKind::SequenceConstruct {
                        element_type: Box::new(element_type.clone()),
                        length: *dst_length,
                    };
                    construct.inputs = projected_elements;
                    construct.outputs = vec![output];
                    rewritten.push(construct);
                    record_vm_sequence_copy_lowering(
                        &mut transformations,
                        &instruction,
                        construct_id,
                        &target,
                        &implementation,
                        "vm-static-exact-sequence-copy-to-project-construct/1",
                    );
                    lowered_count += 1;
                    continue;
                }

                let mut current_sequence = instruction.inputs[0].clone();
                let copy_length = u64::try_from(length).expect("length was bounded by u32");
                for offset in 0..copy_length {
                    let destination_index = push_vm_sequence_copy_constant(
                        &mut rewritten,
                        &owner,
                        &mut ordinal,
                        &block_identity,
                        u64::try_from(dst_at + u128::from(offset))
                            .expect("destination is u32 bounded"),
                    );
                    let source_index = push_vm_sequence_copy_constant(
                        &mut rewritten,
                        &owner,
                        &mut ordinal,
                        &block_identity,
                        u64::try_from(src_at + u128::from(offset)).expect("source is u32 bounded"),
                    );
                    let projection_id = vm_sequence_copy_id(&owner, "project", ordinal);
                    ordinal += 1;
                    let projected_value = SsaValue {
                        identity: vm_sequence_copy_id(&owner, "project-value", ordinal),
                        semantic_identity: None,
                        ty: element_type.clone(),
                        producer: Some(projection_id.clone()),
                        block: block_identity.clone(),
                    };
                    let projected_value_id = projected_value.identity.clone();
                    rewritten.push(vm_sequence_copy_instruction(
                        projection_id,
                        SsaInstructionKind::SequenceProject {
                            bound: src_bound.clone(),
                            evidence: BoundsEvidence::StaticExact,
                        },
                        vec![instruction.inputs[2].clone(), source_index],
                        vec![projected_value],
                    ));

                    let final_operation = offset + 1 == copy_length;
                    let replace_id = if final_operation {
                        vm_sequence_copy_id(&owner, "replace-result", ordinal)
                    } else {
                        vm_sequence_copy_id(&owner, "replace", ordinal)
                    };
                    ordinal += 1;
                    let mut output = if final_operation {
                        instruction.outputs[0].clone()
                    } else {
                        SsaValue {
                            identity: vm_sequence_copy_id(&owner, "replace-value", ordinal),
                            semantic_identity: None,
                            ty: expected_type.clone(),
                            producer: Some(replace_id.clone()),
                            block: block_identity.clone(),
                        }
                    };
                    output.producer = Some(replace_id.clone());
                    let replace = if final_operation {
                        let mut lowered = instruction.clone();
                        lowered.identity = replace_id.clone();
                        lowered.kind = SsaInstructionKind::SequenceReplace {
                            element_type: element_type.clone(),
                            bound: dst_bound.clone(),
                            evidence: BoundsEvidence::StaticExact,
                        };
                        lowered.inputs = vec![
                            current_sequence.clone(),
                            destination_index,
                            projected_value_id,
                        ];
                        lowered.outputs = vec![output];
                        lowered
                    } else {
                        vm_sequence_copy_instruction(
                            replace_id.clone(),
                            SsaInstructionKind::SequenceReplace {
                                element_type: element_type.clone(),
                                bound: dst_bound.clone(),
                                evidence: BoundsEvidence::StaticExact,
                            },
                            vec![
                                current_sequence.clone(),
                                destination_index,
                                projected_value_id,
                            ],
                            vec![output],
                        )
                    };
                    current_sequence = replace.outputs[0].identity.clone();
                    rewritten.push(replace);
                }
                let final_identity = current_sequence;
                record_vm_sequence_copy_lowering(
                    &mut transformations,
                    &instruction,
                    final_identity,
                    &target,
                    &implementation,
                    "vm-static-exact-sequence-copy-to-project-replace/1",
                );
                lowered_count += 1;
            }
            block.instructions = rewritten;
        }
    }
    ssa.transformations.extend(transformations);
    Ok(lowered_count)
}

fn vm_sequence_copy_id(
    owner: &mncs_model::SemanticId,
    role: &str,
    ordinal: u64,
) -> mncs_model::SemanticId {
    let material = format!("{}:{role}:{ordinal}", owner.0);
    mncs_model::SemanticId(format!(
        "mncs:0.2:ssa:vm-sequence-copy:{}",
        mncs_model::sha256_hex(material.as_bytes())
    ))
}

fn push_vm_sequence_copy_constant(
    rewritten: &mut Vec<SsaInstruction>,
    owner: &mncs_model::SemanticId,
    ordinal: &mut u64,
    block: &mncs_model::SemanticId,
    value: u64,
) -> mncs_model::SemanticId {
    let identity = vm_sequence_copy_id(owner, "constant", *ordinal);
    *ordinal += 1;
    let output = SsaValue {
        identity: vm_sequence_copy_id(owner, "constant-value", *ordinal),
        semantic_identity: None,
        ty: BodyType::Integer(mncs_model::IntegerType {
            bits: 64,
            signed: false,
        }),
        producer: Some(identity.clone()),
        block: block.clone(),
    };
    let value_identity = output.identity.clone();
    rewritten.push(vm_sequence_copy_instruction(
        identity,
        SsaInstructionKind::Constant {
            value: i128::from(value),
            ty: output.ty.clone(),
        },
        Vec::new(),
        vec![output],
    ));
    value_identity
}

fn vm_sequence_copy_instruction(
    identity: mncs_model::SemanticId,
    kind: SsaInstructionKind,
    inputs: Vec<mncs_model::SemanticId>,
    outputs: Vec<SsaValue>,
) -> SsaInstruction {
    SsaInstruction {
        identity,
        semantic_identity: None,
        hir_identity: None,
        kind,
        inputs,
        outputs,
        effects: Vec::new(),
        capability_uses: Vec::new(),
        contracts: Vec::new(),
        assumptions: Vec::new(),
        evidence: Vec::new(),
        machine_intent: None,
        obligations: Vec::new(),
        failure: None,
    }
}

fn record_vm_sequence_copy_lowering(
    records: &mut Vec<mncs_model::TransformationRecord>,
    input: &SsaInstruction,
    output: mncs_model::SemanticId,
    target: &TargetIdentity,
    implementation: &mncs_model::SemanticId,
    rule: &str,
) {
    let operation = input
        .semantic_identity
        .clone()
        .unwrap_or_else(|| input.identity.clone());
    let mut record = mncs_model::TransformationRecord::new(
        input.identity.clone(),
        output,
        rule.to_owned(),
        operation,
        target.clone(),
        implementation.clone(),
    );
    record.obligations_required.clone_from(&input.obligations);
    record.evidence_consumed.clone_from(&input.evidence);
    record
        .backend_promises_permitted
        .push("functional-exact-bound-span-copy".to_owned());
    records.push(record);
}

fn emit_vm_artifact_scoped(
    compiler: &ReferenceCompiler,
    program: &Program,
    producer: Option<&str>,
    mut source_map: Option<mncs_compiler::ExecutionSourceMap>,
    selected_exports: Option<&BTreeSet<(String, String)>>,
    selected_generic_bases: Option<&BTreeSet<(String, String)>>,
) -> Result<
    (
        serde_json::Value,
        Option<mncs_compiler::ExecutionSourceMap>,
        Vec<String>,
        Vec<CapabilityDispatchBinding>,
    ),
    String,
> {
    let lowering_program = match selected_exports {
        Some(selected) => Some(application_lowering_program(program, selected)?),
        None => None,
    };
    let lowering_program = lowering_program.as_ref().unwrap_or(program);
    let emit: BTreeSet<ArtifactRepresentation> =
        [ArtifactRepresentation::Ssa].into_iter().collect();
    let request = compiler.request_for_program(lowering_program, emit, None);
    let mut result = compiler.compile(request, lowering_program);
    if result.status == CompilationStatus::Failed {
        return Err(format!(
            "direct vm emission failed for {}: {:?}",
            lowering_program.module, result.diagnostics
        ));
    }
    let ssa = result.emissions.ssa.take().expect("ssa emission present");
    assert!(
        ssa.identity_is_valid(),
        "selected ssa identity is valid for {}",
        program.module
    );
    let generic_rows = generic_entrypoint_rows(program);
    let mut unselected_unresolved: Vec<String> = Vec::new();
    if let Some(selected) = selected_exports {
        for function in &program.functions {
            let namespace = function.identity_namespace(&program.module);
            if !selected.contains(&(namespace.to_owned(), function.name.clone()))
                && resolve_callable(&ssa, namespace, &function.name).is_none()
            {
                unselected_unresolved.push(format!(
                    "export {namespace}::{}: no ssa instance",
                    function.name
                ));
            }
        }
    }
    if let Some(selected) = selected_generic_bases {
        for row in &generic_rows {
            if !selected.contains(&(row.0.clone(), row.1.clone()))
                && resolve_generic_entry(&ssa, row).is_none()
            {
                unselected_unresolved.push(format!(
                    "generic {}::{}({}): no ssa instance",
                    row.0,
                    row.1,
                    row.2.join(", ")
                ));
            }
        }
    }
    let mut execution_ssa = match selected_exports {
        Some(roots) => application_ssa_closure(ssa, lowering_program, roots)?,
        None => ssa,
    };
    let application_closure_fingerprint =
        if selected_exports.is_some() {
            Some(execution_ssa.fingerprint().map_err(|error| {
                format!("projected application SSA fingerprint failed: {error}")
            })?)
        } else {
            None
        };
    let static_sequence_copies_lowered = lower_vm_static_sequence_copies(&mut execution_ssa)?;
    let vm_lowering_fingerprint = (static_sequence_copies_lowered > 0)
        .then(|| execution_ssa.fingerprint())
        .transpose()
        .map_err(|error| format!("VM-lowered application SSA fingerprint failed: {error}"))?;
    let capability_bindings = capability_dispatch_bindings(&execution_ssa)?;
    let selected = result
        .artifacts
        .iter()
        .find(|artifact| artifact.representation == ArtifactRepresentation::SelectedSsa)
        .expect("selected ssa ref present")
        .clone();

    // Exports mirror the research adapter exactly: every program
    // function name in order (informational; may repeat across linked
    // modules).
    let exports: Vec<String> = program
        .functions
        .iter()
        .filter(|function| {
            selected_exports.is_none_or(|selected| {
                selected.contains(&(
                    function.identity_namespace(&program.module).to_owned(),
                    function.name.clone(),
                ))
            })
        })
        .map(|function| function.name.clone())
        .collect();
    let mut unsupported: Vec<String> = Vec::new();
    // Callables resolve per (namespace, name) function, not per bare
    // name: linked programs routinely define one helper name in
    // several modules (segment's `next_token` wraps lexer's), and
    // only the module-qualified pair routes precisely. This is
    // stricter than the migration adapter's bare-name uniqueness
    // rule, which refuses such programs outright.
    let test_bindings: BTreeMap<_, _> = mncs_codegen::language_owned_callable_bindings(program)
        .into_iter()
        .filter(|row| row.test_case_identity.is_some())
        .map(|row| ((row.module.clone(), row.function.clone()), row))
        .collect();
    let mut callables: Vec<CallableEntry> = Vec::with_capacity(program.functions.len());
    for function in &program.functions {
        let namespace = function.identity_namespace(&program.module);
        let selected = selected_exports.is_none_or(|selected| {
            selected.contains(&(namespace.to_owned(), function.name.clone()))
        });
        if !selected {
            continue;
        }
        match resolve_callable(&execution_ssa, namespace, &function.name) {
            Some(mut entry) => {
                entry.test_binding = test_bindings
                    .get(&(namespace.to_owned(), function.name.clone()))
                    .cloned();
                callables.push(entry);
            }
            None => unsupported.push(format!(
                "export {namespace}::{}: no ssa instance",
                function.name
            )),
        }
    }

    let mut generic_entrypoints: Vec<GenericEntrypoint> = Vec::new();
    for row in generic_rows {
        let selected = selected_generic_bases
            .is_none_or(|selected| selected.contains(&(row.0.clone(), row.1.clone())));
        if !selected {
            continue;
        }
        match resolve_generic_entry(&execution_ssa, &row) {
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

    let mut capability_set = BTreeSet::new();
    for function in &execution_ssa.functions {
        for block in &function.blocks {
            for instruction in &block.instructions {
                capability_set.extend(
                    instruction
                        .capability_uses
                        .iter()
                        .map(|usage| usage.capability.0.clone()),
                );
                if let SsaInstructionKind::Call {
                    required_capabilities,
                    ..
                } = &instruction.kind
                {
                    capability_set.extend(
                        required_capabilities
                            .iter()
                            .map(|identity| identity.0.clone()),
                    );
                }
            }
        }
    }
    let capabilities: Vec<String> = capability_set.into_iter().collect();

    // Runtime events name selected SSA operations. Preserve the original
    // semantic rows and add exact compiler-owned SSA -> source correspondences.
    // Imported/generic operations without a root-source correspondence stay absent.
    if let Some(map) = &mut source_map {
        let original: BTreeMap<_, _> = map
            .operations
            .iter()
            .map(|row| (row.identity.clone(), row.clone()))
            .collect();
        let functions: BTreeSet<_> = map
            .functions
            .iter()
            .map(|row| row.identity.clone())
            .collect();
        for function in &execution_ssa.functions {
            if !functions.contains(&function.semantic_identity) {
                continue;
            }
            for block in &function.blocks {
                map.blocks.push(mncs_compiler::ExecutionSourceBlock {
                    identity: block.identity.clone(),
                    function_identity: function.semantic_identity.clone(),
                    source_span: None,
                });
                for instruction in &block.instructions {
                    let Some(origin) = instruction
                        .semantic_identity
                        .as_ref()
                        .and_then(|id| original.get(id))
                    else {
                        continue;
                    };
                    map.operations
                        .push(mncs_compiler::ExecutionSourceOperation {
                            identity: instruction.identity.clone(),
                            function_identity: function.semantic_identity.clone(),
                            block_identity: block.identity.clone(),
                            source_span: origin.source_span,
                            synthetic: origin.synthetic,
                            correspondence: format!("selected-ssa-from:{}", origin.identity.0),
                        });
                }
            }
        }
        map.identity = format!("mncs:compiler:execution-source-map:{}", map.fingerprint());
        assert!(map.identity_is_valid());
    }

    let mut lowering_refs = vec![result.identity.0.clone(), selected.identity.0.clone()];
    lowering_refs.extend(stage0_provenance_refs());
    if let Some(identity) = producer {
        lowering_refs.push(format!("compiler-producer:{identity}"));
    }
    if let Some(map) = &source_map {
        lowering_refs.push(format!("source-map:{}", map.identity));
    }
    if let Some(closure_fingerprint) = application_closure_fingerprint {
        lowering_refs.push(format!("application-ssa-closure/1:{closure_fingerprint}"));
    }
    if let Some(vm_lowering_fingerprint) = vm_lowering_fingerprint {
        lowering_refs.push(format!(
            "vm-static-exact-sequence-copy-lowering/2:{vm_lowering_fingerprint}"
        ));
    }

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
            ssa_schema: execution_ssa.schema_version.clone(),
            module: mncs_vm_artifact_codec::freeze(&execution_ssa)
                .expect("selected application SSA freezes"),
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
    unselected_unresolved.sort();
    unselected_unresolved.dedup();
    let canonical = serde_json::to_vec(&sealed).expect("artifact serializes");
    sealed.artifact_id = artifact_id_of(&canonical);
    Ok((
        serde_json::to_value(&sealed).expect("sealed artifact serializes"),
        source_map,
        unselected_unresolved,
        capability_bindings,
    ))
}

fn capability_dispatch_bindings(ssa: &SsaModule) -> Result<Vec<CapabilityDispatchBinding>, String> {
    let mut bindings = BTreeSet::new();
    for function in &ssa.functions {
        for block in &function.blocks {
            for instruction in &block.instructions {
                let function_identity = function.semantic_identity.0.clone();
                let instruction_identity = instruction.identity.0.clone();
                let source_operation_identity = instruction
                    .semantic_identity
                    .as_ref()
                    .map(|identity| identity.0.clone());
                match &instruction.kind {
                    SsaInstructionKind::HostCall {
                        capability,
                        operation,
                    } => {
                        if instruction.capability_uses.len() != 1 {
                            return Err(format!(
                                "HostCall {} has {} canonical capability uses; expected exactly one",
                                instruction_identity,
                                instruction.capability_uses.len()
                            ));
                        }
                        let usage = &instruction.capability_uses[0];
                        bindings.insert(CapabilityDispatchBinding {
                            capability_identity: usage.capability.0.clone(),
                            runtime_capability_key: capability.clone(),
                            runtime_key_kind: "source-alias".to_owned(),
                            operation: operation.clone(),
                            function_identity,
                            instruction_identity,
                            source_operation_identity,
                        });
                    }
                    SsaInstructionKind::Call {
                        required_capabilities,
                        ..
                    } => {
                        for identity in required_capabilities {
                            bindings.insert(CapabilityDispatchBinding {
                                capability_identity: identity.0.clone(),
                                runtime_capability_key: identity.0.clone(),
                                runtime_key_kind: "canonical-identity".to_owned(),
                                operation: "call-admission".to_owned(),
                                function_identity: function_identity.clone(),
                                instruction_identity: instruction_identity.clone(),
                                source_operation_identity: source_operation_identity.clone(),
                            });
                        }
                    }
                    _ => {
                        let operation = if matches!(instruction.kind, SsaInstructionKind::Effect) {
                            instruction
                                .effects
                                .first()
                                .map(|effect| format!("effect:{}", effect.0))
                                .unwrap_or_else(|| "effect".to_owned())
                        } else {
                            "instruction-capability".to_owned()
                        };
                        for usage in &instruction.capability_uses {
                            bindings.insert(CapabilityDispatchBinding {
                                capability_identity: usage.capability.0.clone(),
                                runtime_capability_key: usage.capability.0.clone(),
                                runtime_key_kind: "canonical-identity".to_owned(),
                                operation: operation.clone(),
                                function_identity: function_identity.clone(),
                                instruction_identity: instruction_identity.clone(),
                                source_operation_identity: source_operation_identity.clone(),
                            });
                        }
                    }
                }
            }
        }
    }
    Ok(bindings.into_iter().collect())
}

/// Bind one module-qualified function to its SSA instance. Panics
/// when one qualified pair names several SSA functions (test
/// transport is loud); pairs without an SSA instance (generics
/// without a compiled instance here) resolve to `None` for explicit
/// recording.
fn resolve_callable(ssa: &SsaModule, namespace: &str, name: &str) -> Option<CallableEntry> {
    let id = mncs_model::function_id(namespace, name);
    let mut candidates = Vec::new();
    for function in &ssa.functions {
        if function.semantic_identity == id {
            candidates.push(function.identity.0.clone());
        }
    }
    match candidates.len() {
        1 => Some(CallableEntry {
            module: namespace.to_owned(),
            name: name.to_owned(),
            function: candidates.pop().unwrap_or_default(),
            semantic: None,
            test_binding: None,
        }),
        0 => None,
        _ => panic!("callable {namespace}::{name} is ambiguous across ssa functions"),
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

#[cfg(test)]
mod application_closure_tests {
    use super::{
        application_lowering_program, application_ssa_closure,
        emit_vm_application_artifact_for_callables_with_producer,
        emit_vm_application_artifact_with_producer, emit_vm_artifact_with_producer,
    };
    use mncs_compiler::ReferenceCompiler;
    use mncs_model::{SsaInstructionKind, SsaModule};
    use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
    use std::collections::BTreeSet;

    #[test]
    fn application_ssa_retains_only_selected_and_nested_record_types() {
        let source = r#"
            mncs 0.18;
            module closure.records;
            record Inner { value: u64 }
            record Outer { inner: Inner }
            record Spare { enabled: bool }
            fn entry(value: u64) -> (result: Outer) { return construct(value); }
            fn construct(value: u64) -> (result: Outer) {
                return Outer { inner: Inner { value: value } };
            }
            fn unused(value: Spare) -> (result: Spare) { return value; }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-records",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let roots = BTreeSet::from([("closure.records".to_owned(), "entry".to_owned())]);

        let full = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("full canonical VM artifact");
        let (application, unresolved, _) = emit_vm_application_artifact_with_producer(
            &compiler,
            program,
            None,
            "closure.records",
            "entry",
            &BTreeSet::new(),
        )
        .expect("application entry artifact");
        assert!(
            unresolved.is_empty(),
            "unexpected unresolved exports: {unresolved:?}"
        );

        let full_module: SsaModule = mncs_vm_artifact_codec::deserialize(
            full["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode full canonical SSA module");
        let projected: SsaModule = mncs_vm_artifact_codec::deserialize(
            application["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode projected canonical SSA closure");
        let full_record_names: BTreeSet<String> = full_module
            .record_types
            .iter()
            .map(|record| record.name.clone())
            .collect();
        let projected_record_names: BTreeSet<String> = projected
            .record_types
            .iter()
            .map(|record| record.name.clone())
            .collect();
        assert_eq!(
            full_record_names,
            BTreeSet::from(["Inner".to_owned(), "Outer".to_owned(), "Spare".to_owned()])
        );
        assert_eq!(
            projected_record_names,
            BTreeSet::from(["Inner".to_owned(), "Outer".to_owned()])
        );
        assert!(
            projected.validate().valid,
            "projected SSA diagnostics: {:?}",
            projected.validate().errors
        );

        let mut incomplete = full_module;
        incomplete
            .record_types
            .retain(|record| record.name != "Inner");
        let error = application_ssa_closure(incomplete, program, &roots)
            .expect_err("a selected closure with a missing nested record must fail closed");
        assert!(
            error.contains("missing record type"),
            "unexpected closure error: {error}"
        );
    }

    #[test]
    fn application_artifact_contains_only_the_transitive_entry_closure() {
        let source = r#"
            mncs 0.18;
            module closure.sample;
            fn entry() -> (result: u64) { return helper(); }
            fn helper() -> (result: u64) { return leaf(); }
            fn leaf() -> (result: u64) { return 7; }
            fn unused() -> (result: u64) { return 99; }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-sample",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");

        let full = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("full canonical VM artifact");
        let (application, unresolved, _) = emit_vm_application_artifact_with_producer(
            &compiler,
            program,
            None,
            "closure.sample",
            "entry",
            &BTreeSet::new(),
        )
        .expect("application entry artifact");
        assert!(
            unresolved.is_empty(),
            "unexpected unselected unresolved exports: {unresolved:?}"
        );

        let module = application["code"]["mncs-selected-ssa"]["module"].clone();
        let projected: SsaModule = mncs_vm_artifact_codec::deserialize(module)
            .expect("decode projected canonical SSA closure");
        let full_module: SsaModule = mncs_vm_artifact_codec::deserialize(
            full["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode full canonical SSA module");
        let roots = BTreeSet::from([("closure.sample".to_owned(), "entry".to_owned())]);
        let expected_closure = application_ssa_closure(full_module.clone(), program, &roots)
            .expect("full compiler output projects to the selected application closure");
        assert_eq!(
            projected.functions, expected_closure.functions,
            "selected SSA functions must be identical when unrelated bodies are omitted before lowering"
        );
        assert_eq!(projected.record_types, expected_closure.record_types);
        assert_eq!(projected.obligations, expected_closure.obligations);
        assert_eq!(projected.transformations, expected_closure.transformations);
        assert_eq!(
            projected.generic_specializations,
            expected_closure.generic_specializations
        );
        let functions: BTreeSet<String> = projected
            .functions
            .iter()
            .map(|function| function.semantic_identity.0.clone())
            .collect();
        assert_eq!(
            functions,
            BTreeSet::from([
                mncs_model::function_id("closure.sample", "entry").0,
                mncs_model::function_id("closure.sample", "helper").0,
                mncs_model::function_id("closure.sample", "leaf").0,
            ])
        );
        assert!(
            projected.validate().valid,
            "projected SSA diagnostics: {:?}",
            projected.validate().errors
        );
        assert!(projected.binding_table.is_none());
        assert!(projected.trace.entries.len() < full_module.trace.entries.len());
        assert!(projected.transformations.len() < full_module.transformations.len());
        assert!(application["source"]["lowering_refs"]
            .as_array()
            .is_some_and(|refs| refs.iter().any(|row| {
                row.as_str()
                    .is_some_and(|value| value.starts_with("application-ssa-closure/1:"))
            })));
        let full_bytes = serde_json::to_vec(&full)
            .expect("serialize full artifact")
            .len();
        let application_bytes = serde_json::to_vec(&application)
            .expect("serialize application artifact")
            .len();
        assert!(
            application_bytes < full_bytes,
            "entry closure should reduce this artifact: app={application_bytes}, full={full_bytes}"
        );
    }

    #[test]
    fn application_lowering_preserves_full_validation_and_only_keeps_reachable_bodies() {
        let source = r#"
            mncs 0.18;
            module closure.lowering;
            fn entry() -> (result: u64) { return helper(); }
            fn helper() -> (result: u64) { return leaf(); }
            fn leaf() -> (result: u64) { return 7; }
            fn unused() -> (result: u64) { return 99; }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-lowering",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let roots = BTreeSet::from([("closure.lowering".to_owned(), "entry".to_owned())]);
        let projected = application_lowering_program(program, &roots)
            .expect("verified application lowering projection");
        let retained: BTreeSet<_> = projected
            .functions
            .iter()
            .filter(|function| function.body.is_some())
            .map(|function| function.name.as_str())
            .collect();
        assert_eq!(retained, BTreeSet::from(["entry", "helper", "leaf"]));
        assert_eq!(projected.functions.len(), program.functions.len());

        let mut invalid = program.clone();
        invalid
            .functions
            .iter_mut()
            .find(|function| function.name == "unused")
            .expect("unused function")
            .body
            .as_mut()
            .expect("unused body")
            .blocks
            .clear();
        let error = application_lowering_program(&invalid, &roots)
            .expect_err("invalid unreachable code must not be masked by backend projection");
        assert!(error.contains("failed semantic validation"), "{error}");
    }

    #[test]
    fn application_artifact_contains_each_selected_callable_closure() {
        let source = r#"
            mncs 0.18;
            module closure.batch;
            fn first() -> (result: u64) { return helper(); }
            fn helper() -> (result: u64) { return 7; }
            fn second() -> (result: u64) { return 5; }
            fn unused() -> (result: u64) { return 99; }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-batch-sample",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let roots = BTreeSet::from([
            ("closure.batch".to_owned(), "first".to_owned()),
            ("closure.batch".to_owned(), "second".to_owned()),
        ]);
        let (artifact, unresolved, _) = emit_vm_application_artifact_for_callables_with_producer(
            &compiler,
            program,
            None,
            &roots,
            &BTreeSet::new(),
        )
        .expect("selected callable set artifact");
        assert!(
            unresolved.is_empty(),
            "unexpected unresolved exports: {unresolved:?}"
        );

        let module = artifact["code"]["mncs-selected-ssa"]["module"].clone();
        let projected: SsaModule = mncs_vm_artifact_codec::deserialize(module)
            .expect("decode projected canonical SSA closure");
        let functions: BTreeSet<String> = projected
            .functions
            .iter()
            .map(|function| function.semantic_identity.0.clone())
            .collect();
        assert_eq!(
            functions,
            BTreeSet::from([
                mncs_model::function_id("closure.batch", "first").0,
                mncs_model::function_id("closure.batch", "helper").0,
                mncs_model::function_id("closure.batch", "second").0,
            ])
        );
        let callables: BTreeSet<String> = artifact["callables"]
            .as_array()
            .expect("selected callables")
            .iter()
            .map(|row| row["name"].as_str().expect("callable name").to_owned())
            .collect();
        assert_eq!(
            callables,
            BTreeSet::from(["first".to_owned(), "second".to_owned()])
        );
        assert!(projected.validate().valid);
    }

    #[test]
    fn canonical_vm_lowers_static_exact_sequence_copies_to_supported_ops() {
        let source = r#"
            mncs 0.18;
            module closure.static_copy;
            fn copy_window(destination: [byte; 8], source: [byte; 8]) -> (result: [byte; 8]) {
                let unchanged: [byte; 8] = copy_span(destination, 8, source, 8, 0);
                return copy_span(unchanged, 2, source, 1, 3);
            }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-static-copy",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let artifact = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("canonical VM artifact with statically lowered span copy");
        let module: SsaModule = mncs_vm_artifact_codec::deserialize(
            artifact["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode canonical selected SSA");
        assert!(
            module.validate().valid,
            "VM-lowered SSA diagnostics: {:?}",
            module.validate().errors
        );
        let instructions = module
            .functions
            .iter()
            .flat_map(|function| function.blocks.iter())
            .flat_map(|block| block.instructions.iter())
            .collect::<Vec<_>>();
        assert!(instructions.iter().all(|instruction| !matches!(
            instruction.kind,
            SsaInstructionKind::SequenceCopy { .. }
        )));
        assert_eq!(
            instructions
                .iter()
                .filter(|instruction| matches!(
                    instruction.kind,
                    SsaInstructionKind::SequenceProject { .. }
                ))
                .count(),
            3
        );
        assert_eq!(
            instructions
                .iter()
                .filter(|instruction| matches!(
                    instruction.kind,
                    SsaInstructionKind::SequenceReplace { .. }
                ))
                .count(),
            3
        );
        assert_eq!(
            instructions
                .iter()
                .filter(|instruction| matches!(instruction.kind, SsaInstructionKind::Select { .. }))
                .count(),
            1,
            "the empty span lowers to a value-preserving selection"
        );
        assert_eq!(
            module
                .transformations
                .iter()
                .filter(|record| record.rule == "vm-static-exact-sequence-copy-to-project-replace/1")
                .count(),
            2
        );
        assert!(artifact["source"]["lowering_refs"]
            .as_array()
            .is_some_and(|refs| refs.iter().any(|row| {
                row.as_str().is_some_and(|value| {
                    value.starts_with("vm-static-exact-sequence-copy-lowering/2:")
                })
            })));
    }

    #[test]
    fn canonical_vm_rebuilds_bounded_large_static_exact_sequence_copies() {
        let source = r#"
            mncs 0.18;
            module closure.large_static_copy;
            fn copy_window(
                destination: [byte; 320],
                source: [byte; 320]
            ) -> (result: [byte; 320]) {
                return copy_span(destination, 10, source, 5, 300);
            }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-large-static-copy",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let artifact = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("canonical VM artifact with bounded large span-copy reconstruction");
        let module: SsaModule = mncs_vm_artifact_codec::deserialize(
            artifact["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode canonical selected SSA");
        assert!(
            module.validate().valid,
            "VM-lowered SSA diagnostics: {:?}",
            module.validate().errors
        );
        let instructions = module
            .functions
            .iter()
            .flat_map(|function| function.blocks.iter())
            .flat_map(|block| block.instructions.iter())
            .collect::<Vec<_>>();
        assert!(instructions.iter().all(|instruction| !matches!(
            instruction.kind,
            SsaInstructionKind::SequenceCopy { .. }
        )));
        assert_eq!(
            instructions
                .iter()
                .filter(|instruction| matches!(
                    instruction.kind,
                    SsaInstructionKind::SequenceProject { .. }
                ))
                .count(),
            320
        );
        assert_eq!(
            instructions
                .iter()
                .filter(|instruction| matches!(
                    instruction.kind,
                    SsaInstructionKind::SequenceConstruct { .. }
                ))
                .count(),
            1
        );
        assert_eq!(
            module
                .transformations
                .iter()
                .filter(|record| {
                    record.rule == "vm-static-exact-sequence-copy-to-project-construct/1"
                })
                .count(),
            1
        );
        assert!(artifact["source"]["lowering_refs"]
            .as_array()
            .is_some_and(|refs| refs.iter().any(|row| {
                row.as_str().is_some_and(|value| {
                    value.starts_with("vm-static-exact-sequence-copy-lowering/2:")
                })
            })));
    }

    #[test]
    fn canonical_vm_keeps_oversized_static_sequence_copies_explicit() {
        let source = r#"
            mncs 0.18;
            module closure.oversized_static_copy;
            fn copy_window(
                destination: [byte; 1024],
                source: [byte; 1024]
            ) -> (result: [byte; 1024]) {
                return copy_span(destination, 10, source, 5, 300);
            }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-oversized-static-copy",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let artifact = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("canonical VM artifact retains an oversized exact span copy");
        let module: SsaModule = mncs_vm_artifact_codec::deserialize(
            artifact["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode canonical selected SSA");
        assert!(module.validate().valid);
        assert!(module.functions.iter().any(|function| {
            function.blocks.iter().any(|block| {
                block.instructions.iter().any(|instruction| {
                    matches!(instruction.kind, SsaInstructionKind::SequenceCopy { .. })
                })
            })
        }));
        assert!(!module.transformations.iter().any(|record| {
            record
                .rule
                .starts_with("vm-static-exact-sequence-copy-to-project-")
        }));
        assert!(!artifact["source"]["lowering_refs"]
            .as_array()
            .is_some_and(|refs| refs.iter().any(|row| {
                row.as_str().is_some_and(|value| {
                    value.starts_with("vm-static-exact-sequence-copy-lowering/")
                })
            })));
    }

    #[test]
    fn canonical_vm_keeps_runtime_checked_sequence_copies_explicit() {
        let source = r#"
            mncs 0.18;
            module closure.dynamic_copy;
            fn copy_window(
                destination: [byte; 8],
                destination_at: u64,
                source: [byte; up_to 8],
                source_at: u64,
                length: u64
            ) -> (result: [byte; 8]) {
                return copy_span(destination, destination_at, source, source_at, length);
            }
        "#;
        let compiler = ReferenceCompiler::default();
        let frontend = compiler.front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "closure-dynamic-copy",
            source.to_owned(),
        ));
        assert!(
            frontend.is_valid(),
            "frontend diagnostics: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("validated Program");
        let artifact = emit_vm_artifact_with_producer(&compiler, program, None)
            .expect("canonical VM artifact retains runtime-checked span copy");
        let module: SsaModule = mncs_vm_artifact_codec::deserialize(
            artifact["code"]["mncs-selected-ssa"]["module"].clone(),
        )
        .expect("decode canonical selected SSA");
        assert!(module.validate().valid);
        assert!(module.functions.iter().any(|function| {
            function.blocks.iter().any(|block| {
                block.instructions.iter().any(|instruction| {
                    matches!(instruction.kind, SsaInstructionKind::SequenceCopy { .. })
                })
            })
        }));
        assert!(!module
            .transformations
            .iter()
            .any(|record| { record.rule == "vm-static-exact-sequence-copy-to-project-replace/1" }));
    }
}
