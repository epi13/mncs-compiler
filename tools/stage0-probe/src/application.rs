//! Versioned source-snapshot application build contract for the canonical VM.
//!
//! This is an adapter around the pinned Stage-0 compiler and the existing
//! compiler-owned mncs.vm.artifact/1 emitter. The application contract
//! records provenance and obligations; it does not replace the VM artifact as
//! the executable authority or implement language semantics here.
use crate::{vm_emit, ToolchainIdentity};
use mncs_compiler::{ModuleResolution, ModuleResolver, ReferenceCompiler};
use mncs_model::{ArtifactRepresentation, ExecutionTypeArgument, HostGenericSeedRequest};
use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

static CACHE_NONCE: AtomicU64 = AtomicU64::new(0);
static PRODUCER_IDENTITY: OnceLock<(String, Value)> = OnceLock::new();

const CONTRACT: &str = "mncs.compiler.application-build/4";
const CAPABILITY_DISPATCH_SCHEMA: &str = "mncs.compiler.capability-dispatch/1";
const REQUEST: &str = "mncs.compiler.application-build-request/1";
const VM_TARGET: &str = "mncs-vm-0.1";
const MAX_MODULES: usize = 64;
const MAX_SOURCE_BYTES: usize = 4 * 1024 * 1024;
const MAX_GENERIC_SEEDS: usize = 256;

struct ApplicationSources(BTreeMap<String, SourceEnvelope>);

impl ModuleResolver for ApplicationSources {
    fn resolve(&self, name: &str) -> Option<SourceEnvelope> {
        self.0.get(name).cloned()
    }
}

fn envelope(source: &str) -> SourceEnvelope {
    SourceEnvelope::inline(
        SourceArtifactKind::Program,
        "application",
        source.to_owned(),
    )
}

fn push_part(output: &mut Vec<u8>, value: &[u8]) {
    output.extend_from_slice(&(value.len() as u64).to_be_bytes());
    output.extend_from_slice(value);
}

fn source_snapshot_identity(
    root_id: &str,
    root_source: &str,
    modules: &BTreeMap<String, String>,
) -> String {
    let mut bytes = b"mncs.compiler.source-snapshot/1\0".to_vec();
    push_part(&mut bytes, root_id.as_bytes());
    push_part(&mut bytes, root_source.as_bytes());
    for (module, source) in modules {
        push_part(&mut bytes, module.as_bytes());
        push_part(&mut bytes, source.as_bytes());
    }
    format!("sha256:{}", mncs_model::sha256_hex(&bytes))
}

fn producer_identity() -> (String, Value) {
    PRODUCER_IDENTITY
        .get_or_init(|| {
            let executable = std::env::current_exe().ok();
            let executable_sha256 = executable
                .as_ref()
                .and_then(|path| std::fs::read(path).ok())
                .map(|bytes| format!("sha256:{}", mncs_model::sha256_hex(&bytes)));
            let identity = executable_sha256
                .clone()
                .unwrap_or_else(|| "unknown".to_owned());
            let details = json!({
                "kind": "mncs.stage0-reference-compiler",
                "probe_executable": executable.map(|path| path.display().to_string()),
                "probe_executable_sha256": executable_sha256,
            });
            (identity, details)
        })
        .clone()
}

fn error_contract(status: &str, code: &str, message: &str) -> Value {
    json!({
        "schema_version": CONTRACT,
        "status": status,
        "error": { "code": code, "message": message },
        "artifact": null,
        "capability_dispatch": {
            "schema_version": CAPABILITY_DISPATCH_SCHEMA,
            "status": "unknown",
            "bindings": [],
            "unresolved_obligations": [],
        },
        "diagnostics": [],
        "unresolved_obligations": [],
        "unselected_unresolved_exports": [],
    })
}

fn has_exact_keys(value: &Value, keys: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
    })
}

fn parse_generic_seed(value: &Value) -> Result<HostGenericSeedRequest, ()> {
    if !has_exact_keys(value, &["module", "function", "type_arguments"]) {
        return Err(());
    }
    let module = value.get("module").and_then(Value::as_str).ok_or(())?;
    let function = value.get("function").and_then(Value::as_str).ok_or(())?;
    let arguments = value
        .get("type_arguments")
        .and_then(Value::as_array)
        .ok_or(())?;
    let mut type_arguments = Vec::with_capacity(arguments.len());
    for argument in arguments {
        let kind = argument.get("kind").and_then(Value::as_str).ok_or(())?;
        match kind {
            "nat" if has_exact_keys(argument, &["kind", "value"]) => {
                let value = argument
                    .get("value")
                    .and_then(Value::as_u64)
                    .and_then(|value| u32::try_from(value).ok())
                    .ok_or(())?;
                type_arguments.push(ExecutionTypeArgument::Nat { value });
            }
            "type" if has_exact_keys(argument, &["kind", "type"]) => {
                let ty = argument.get("type").and_then(Value::as_str).ok_or(())?;
                type_arguments.push(ExecutionTypeArgument::Type { ty: ty.to_owned() });
            }
            _ => return Err(()),
        }
    }
    Ok(HostGenericSeedRequest::from_request(
        module,
        function,
        &type_arguments,
    ))
}

fn parse_generic_seeds(value: &Value) -> Result<Vec<HostGenericSeedRequest>, ()> {
    let rows = value.as_array().ok_or(())?;
    rows.iter().map(parse_generic_seed).collect()
}

fn application_cache_key(
    request: &Value,
    root_id: &str,
    source_identity: &str,
    modules: &BTreeMap<String, String>,
    generic_seeds: &[HostGenericSeedRequest],
    toolchain: Option<&ToolchainIdentity>,
    probe_identity: &str,
) -> String {
    let module_sources: Vec<Value> = modules
        .iter()
        .map(|(module, source)| {
            json!({
                "module": module,
                "sha256": format!("sha256:{}", mncs_model::sha256_hex(source.as_bytes())),
            })
        })
        .collect();
    let mut generic_seed_keys: Vec<String> = generic_seeds
        .iter()
        .map(HostGenericSeedRequest::seed_key)
        .collect();
    generic_seed_keys.sort();
    generic_seed_keys.dedup();
    let identity = json!({
        "schema_version": "mncs.compiler.application-build-cache-key/3",
        "contract": CONTRACT,
        "source_snapshot_identity": source_identity,
        "root_source_id": root_id,
        "module_sources": module_sources,
        "generic_seeds": generic_seed_keys,
        "compiler_semantics": toolchain
            .and_then(|identity| serde_json::to_value(identity).ok()),
        "compiler_implementation": probe_identity,
        "backend": "mncs-vm-direct",
        "target": request.get("target").cloned().unwrap_or(Value::Null),
        "capabilities": request.get("capabilities").cloned().unwrap_or_else(|| json!([])),
        "effect_assumptions": request.get("effect_assumptions").cloned().unwrap_or_else(|| json!([])),
        "optimization_policy": request.get("optimization_policy").cloned().unwrap_or_else(|| json!("default")),
        "verification_policy": request.get("verification_policy").cloned().unwrap_or_else(|| json!("default")),
        "entry": request.get("entry").cloned().unwrap_or(Value::Null),
    });
    let mut bytes = b"mncs.compiler.application-build-cache/3\0".to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(&identity).unwrap_or_default());
    format!("sha256:{}", mncs_model::sha256_hex(&bytes))
}

fn application_cache_index_key(
    request: &Value,
    root_id: &str,
    root_source: &str,
    generic_seeds: &[HostGenericSeedRequest],
    toolchain: Option<&ToolchainIdentity>,
    probe_identity: &str,
) -> String {
    let mut generic_seed_keys: Vec<String> = generic_seeds
        .iter()
        .map(HostGenericSeedRequest::seed_key)
        .collect();
    generic_seed_keys.sort();
    generic_seed_keys.dedup();
    let identity = json!({
        "schema_version": "mncs.compiler.application-build-cache-index-key/2",
        "contract": CONTRACT,
        "root_source_id": root_id,
        "root_source_sha256": format!("sha256:{}", mncs_model::sha256_hex(root_source.as_bytes())),
        "generic_seeds": generic_seed_keys,
        "compiler_semantics": toolchain
            .and_then(|identity| serde_json::to_value(identity).ok()),
        "compiler_implementation": probe_identity,
        "backend": "mncs-vm-direct",
        "target": request.get("target").cloned().unwrap_or(Value::Null),
        "capabilities": request.get("capabilities").cloned().unwrap_or_else(|| json!([])),
        "effect_assumptions": request.get("effect_assumptions").cloned().unwrap_or_else(|| json!([])),
        "optimization_policy": request.get("optimization_policy").cloned().unwrap_or_else(|| json!("default")),
        "verification_policy": request.get("verification_policy").cloned().unwrap_or_else(|| json!("default")),
        "entry": request.get("entry").cloned().unwrap_or(Value::Null),
    });
    let mut bytes = b"mncs.compiler.application-build-cache-index/2\0".to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(&identity).unwrap_or_default());
    format!("sha256:{}", mncs_model::sha256_hex(&bytes))
}

fn application_cache_path(key: &str) -> Option<PathBuf> {
    let root = std::env::var_os("MNCS_PROBE_CACHE_DIR")?;
    if root.is_empty() {
        return None;
    }
    let hex = key.strip_prefix("sha256:")?;
    Some(
        PathBuf::from(root)
            .join("application-builds")
            .join(format!("{hex}.json")),
    )
}

fn application_cache_index_path(key: &str) -> Option<PathBuf> {
    let root = std::env::var_os("MNCS_PROBE_CACHE_DIR")?;
    if root.is_empty() {
        return None;
    }
    let hex = key.strip_prefix("sha256:")?;
    Some(
        PathBuf::from(root)
            .join("application-build-indexes")
            .join(format!("{hex}.json")),
    )
}

fn annotate_cache(
    response: Value,
    key: &str,
    status: &str,
    publication: &str,
    reused_units: &[&str],
    recomputed_units: &[&str],
) -> Value {
    let (reuse_basis, reused_from_key) = if status == "hit" {
        ("exact_source_snapshot", Some(key))
    } else {
        ("none", None)
    };
    annotate_cache_with_basis(
        response,
        key,
        status,
        publication,
        reuse_basis,
        reused_from_key,
        reused_units,
        recomputed_units,
    )
}

fn annotate_cache_with_basis(
    mut response: Value,
    key: &str,
    status: &str,
    publication: &str,
    reuse_basis: &str,
    reused_from_key: Option<&str>,
    reused_units: &[&str],
    recomputed_units: &[&str],
) -> Value {
    if let Some(object) = response.as_object_mut() {
        object.insert(
            "cache".to_owned(),
            json!({
                "schema_version": "mncs.compiler.application-build-cache-observation/3",
                "key": key,
                "status": status,
                "publication": publication,
                "reuse_basis": reuse_basis,
                "reused_from_key": reused_from_key,
                "reused_units": reused_units,
                "recomputed_units": recomputed_units,
            }),
        );
    }
    response
}

fn read_application_cache(
    path: &PathBuf,
    key: &str,
    expected_source_identity: Option<&str>,
    probe_identity: &str,
    toolchain: Option<&ToolchainIdentity>,
) -> Option<Value> {
    let entry: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if entry.get("schema_version").and_then(Value::as_str)
        != Some("mncs.compiler.application-build-cache-entry/3")
        || entry.get("key").and_then(Value::as_str) != Some(key)
    {
        return None;
    }
    let response = entry.get("response")?.clone();
    let response_bytes = serde_json::to_vec(&response).ok()?;
    let response_digest = format!("sha256:{}", mncs_model::sha256_hex(&response_bytes));
    let cached_source_identity = response
        .pointer("/source_snapshot/identity")
        .and_then(Value::as_str)?;
    if entry.get("response_sha256").and_then(Value::as_str) != Some(response_digest.as_str())
        || !cached_source_identity.starts_with("sha256:")
        || expected_source_identity.is_some_and(|expected| expected != cached_source_identity)
        || response
            .pointer("/producer/probe/probe_executable_sha256")
            .and_then(Value::as_str)
            != Some(probe_identity)
        || response
            .pointer("/artifact/schema_version")
            .and_then(Value::as_str)
            != Some("mncs.vm.artifact/1")
        || response
            .pointer("/artifact/value/schema_version")
            .and_then(Value::as_str)
            != Some("mncs.vm.artifact/1")
        || response.pointer("/artifact/identity").cloned()
            != response.pointer("/artifact/value/artifact_id").cloned()
        || response.pointer("/entry/status").and_then(Value::as_str) != Some("verified")
    {
        return None;
    }
    let expected_toolchain = toolchain.and_then(|identity| serde_json::to_value(identity).ok());
    if response.pointer("/producer/stage0_toolchain").cloned() != expected_toolchain {
        return None;
    }
    Some(response)
}

fn cached_application_matches_dependency_closure(
    response: &Value,
    root_id: &str,
    root_source: &str,
    modules: &BTreeMap<String, String>,
) -> bool {
    let Some(files) = response
        .pointer("/source_snapshot/files")
        .and_then(Value::as_array)
    else {
        return false;
    };
    let root_hash = format!("sha256:{}", mncs_model::sha256_hex(root_source.as_bytes()));
    let mut found_root = false;
    let mut cached_modules = BTreeMap::<String, String>::new();
    for file in files {
        let Some(module) = file.get("module").and_then(Value::as_str) else {
            return false;
        };
        let Some(source_id) = file.get("source_id").and_then(Value::as_str) else {
            return false;
        };
        let Some(source_hash) = file.get("sha256").and_then(Value::as_str) else {
            return false;
        };
        if module == "root" {
            if found_root || source_id != root_id || source_hash != root_hash {
                return false;
            }
            found_root = true;
        } else if source_id != module
            || cached_modules
                .insert(module.to_owned(), source_hash.to_owned())
                .is_some()
        {
            return false;
        }
    }
    if !found_root {
        return false;
    }

    let Some(dependencies) = response
        .pointer("/dependency_graph/details/dependencies")
        .and_then(Value::as_array)
    else {
        return false;
    };
    let Some(resolutions) = response
        .pointer("/dependency_graph/details/resolutions")
        .and_then(Value::as_array)
    else {
        return false;
    };
    let declared_dependencies: BTreeSet<String> = dependencies
        .iter()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect();
    if declared_dependencies.len() != dependencies.len() {
        return false;
    }
    let mut resolved_dependencies = BTreeSet::new();
    let mut seen_modules = BTreeSet::new();
    for resolution in resolutions {
        let Some(requested_module) = resolution.get("requested_module").and_then(Value::as_str)
        else {
            return false;
        };
        let Some(module_identity) = resolution.get("module_identity").and_then(Value::as_str)
        else {
            return false;
        };
        if resolution
            .get("source_identity")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
            || resolution
                .get("semantic_fingerprint")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            || !seen_modules.insert(requested_module.to_owned())
        {
            return false;
        }
        let Some(current_source) = modules.get(requested_module) else {
            return false;
        };
        let Some(cached_hash) = cached_modules.get(requested_module) else {
            return false;
        };
        let current_hash = format!(
            "sha256:{}",
            mncs_model::sha256_hex(current_source.as_bytes())
        );
        if cached_hash != &current_hash {
            return false;
        }
        resolved_dependencies.insert(module_identity.to_owned());
    }
    resolved_dependencies == declared_dependencies
}

fn read_application_cache_index(path: &PathBuf, key: &str) -> Option<String> {
    let entry: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if entry.get("schema_version").and_then(Value::as_str)
        != Some("mncs.compiler.application-build-cache-index/2")
        || entry.get("key").and_then(Value::as_str) != Some(key)
    {
        return None;
    }
    let candidate_key = entry.get("candidate_key").and_then(Value::as_str)?;
    let hex = candidate_key.strip_prefix("sha256:")?;
    if hex.len() != 64 || !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    Some(candidate_key.to_owned())
}

/// Aggregate the compiler's canonical root and imported-module semantic
/// fingerprints into one identity for safe backend-artifact reuse. Source
/// identities are deliberately excluded: comment and formatting edits still
/// require a fresh source check, but they need not force VM lowering when all
/// verified semantic facts remain identical.
fn application_semantic_identity(
    program: &mncs_model::Program,
    root_semantic_fingerprint: &str,
    module_resolutions: &[ModuleResolution],
    generic_seeds: &[HostGenericSeedRequest],
) -> Option<String> {
    if root_semantic_fingerprint.is_empty() {
        return None;
    }

    let program_dependencies: BTreeSet<String> = program
        .dependencies
        .iter()
        .map(|identity| identity.0.clone())
        .collect();
    if program_dependencies.len() != program.dependencies.len() {
        return None;
    }

    let mut resolved_modules = BTreeMap::<String, Value>::new();
    let mut resolved_identities = BTreeSet::<String>::new();
    for resolution in module_resolutions {
        let semantic_fingerprint = resolution.semantic_fingerprint.as_deref()?;
        if semantic_fingerprint.is_empty()
            || mncs_model::module_id(&resolution.declared_module).0 != resolution.module_identity.0
        {
            return None;
        }
        let module_identity = resolution.module_identity.0.clone();
        if !resolved_identities.insert(module_identity.clone()) {
            return None;
        }
        let row = json!({
            "requested_module": resolution.requested_module,
            "declared_module": resolution.declared_module,
            "module_identity": module_identity,
            "semantic_fingerprint": semantic_fingerprint,
        });
        if resolved_modules
            .insert(resolution.requested_module.clone(), row)
            .is_some()
        {
            return None;
        }
    }
    if !program_dependencies.is_subset(&resolved_identities) {
        return None;
    }

    let mut generic_seed_keys: Vec<String> = generic_seeds
        .iter()
        .map(HostGenericSeedRequest::seed_key)
        .collect();
    generic_seed_keys.sort();
    generic_seed_keys.dedup();
    let identity = json!({
        "schema_version": "mncs.compiler.application-semantic-identity/1",
        "root_module": program.module,
        "root_semantic_fingerprint": root_semantic_fingerprint,
        "resolved_modules": resolved_modules.into_values().collect::<Vec<_>>(),
        "generic_seed_keys": generic_seed_keys,
    });
    let mut bytes = b"mncs.compiler.application-semantic-identity/1\0".to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(&identity).ok()?);
    Some(format!("sha256:{}", mncs_model::sha256_hex(&bytes)))
}

fn collect_nominal_type_identities(
    ty: &mncs_model::BodyType,
    identities: &mut BTreeSet<mncs_model::SemanticId>,
) {
    match ty {
        mncs_model::BodyType::Record { identity, .. }
        | mncs_model::BodyType::Finite { identity, .. } => {
            identities.insert(identity.clone());
        }
        mncs_model::BodyType::Sequence { element, .. }
        | mncs_model::BodyType::Vector { element, .. } => {
            collect_nominal_type_identities(element, identities);
        }
        _ => {}
    }
}

/// Fingerprint the verified function and nominal-type facts that can affect
/// the selected application entry or an explicit generic seed. The frontend
/// still verifies the complete source snapshot before this value is used;
/// this identity only decides whether the canonical VM artifact can be
/// reused without repeating closure emission and verification.
fn application_executable_closure_identity(
    program: &mncs_model::Program,
    entry_module: &str,
    entry_function: &str,
    generic_seeds: &[HostGenericSeedRequest],
) -> Option<String> {
    let semantic_facts = program.semantic_identities();
    let mut functions_by_identity = BTreeMap::new();
    for function in &program.functions {
        let identity =
            mncs_model::function_id(function.identity_namespace(&program.module), &function.name);
        if functions_by_identity
            .insert(identity.0.clone(), function)
            .is_some()
        {
            return None;
        }
    }

    let mut callable_roots = BTreeSet::new();
    callable_roots.insert(mncs_model::function_id(entry_module, entry_function).0);
    let mut generic_seed_keys = generic_seeds
        .iter()
        .map(HostGenericSeedRequest::seed_key)
        .collect::<Vec<_>>();
    generic_seed_keys.sort();
    generic_seed_keys.dedup();
    for seed in generic_seeds {
        callable_roots.insert(mncs_model::function_id(&seed.module, &seed.function).0);
    }

    let mut pending = callable_roots.clone();
    let mut function_facts = BTreeMap::<String, String>::new();
    let mut nominal_types = BTreeSet::<mncs_model::SemanticId>::new();
    while let Some(identity) = pending.pop_first() {
        if function_facts.contains_key(&identity) {
            continue;
        }
        let function = functions_by_identity.get(&identity)?;
        let semantic_id = mncs_model::SemanticId(identity.clone());
        let fingerprint = semantic_facts.fingerprint(&semantic_id)?.to_owned();
        function_facts.insert(identity.clone(), fingerprint);

        for value in function.inputs.iter().chain(&function.outputs) {
            collect_nominal_type_identities(
                &mncs_model::BodyType::from_program(program, &value.value_type),
                &mut nominal_types,
            );
        }

        let body = function.body.as_ref()?;
        for parameter in &body.parameters {
            collect_nominal_type_identities(&parameter.ty, &mut nominal_types);
        }
        for iteration in &body.bounded_iterations {
            collect_nominal_type_identities(&iteration.state_type, &mut nominal_types);
            for callee in &iteration.callees {
                pending.insert(callee.0.clone());
            }
        }
        for block in &body.blocks {
            for parameter in &block.parameters {
                collect_nominal_type_identities(&parameter.ty, &mut nominal_types);
            }
            for operation in &block.operations {
                for result in &operation.results {
                    collect_nominal_type_identities(&result.ty, &mut nominal_types);
                }
                match &operation.kind {
                    mncs_model::BodyOperationKind::Call { function, .. } => {
                        pending.insert(function.0.clone());
                    }
                    mncs_model::BodyOperationKind::FiniteConstruct { type_identity, .. }
                    | mncs_model::BodyOperationKind::FiniteIsVariant { type_identity, .. }
                    | mncs_model::BodyOperationKind::FinitePayloadProject {
                        type_identity, ..
                    }
                    | mncs_model::BodyOperationKind::RecordConstruct { type_identity, .. }
                    | mncs_model::BodyOperationKind::RecordProject { type_identity, .. } => {
                        nominal_types.insert(type_identity.clone());
                    }
                    _ => {}
                }
            }
        }
        for specialization in &program.generic_specializations {
            if specialization.generic_function.0 == identity {
                pending.insert(specialization.specialization_function.0.clone());
            }
        }
    }

    // A host specialization argument can resolve through declarations that
    // are not present in the generic function's concrete body types. Bind all
    // nominal declarations when explicit seeds are present; this is
    // deliberately conservative until the Language facts API exposes the
    // resolved type arguments directly.
    if !generic_seeds.is_empty() {
        nominal_types.extend(
            program
                .record_types
                .iter()
                .map(|record| record.identity.clone()),
        );
        nominal_types.extend(
            program
                .finite_types
                .iter()
                .map(|finite| finite.identity.clone()),
        );
    }

    let mut type_facts = BTreeMap::<String, String>::new();
    while let Some(identity) = nominal_types.pop_first() {
        if type_facts.contains_key(identity.as_str()) {
            continue;
        }
        let fingerprint = semantic_facts.fingerprint(&identity)?.to_owned();
        if let Some(record) = program
            .record_types
            .iter()
            .find(|row| row.identity == identity)
        {
            for field in &record.fields {
                collect_nominal_type_identities(
                    &mncs_model::BodyType::from_program(program, &field.field_type),
                    &mut nominal_types,
                );
            }
        } else if let Some(finite) = program
            .finite_types
            .iter()
            .find(|row| row.identity == identity)
        {
            for variant in &finite.variants {
                for field in &variant.payload {
                    collect_nominal_type_identities(
                        &mncs_model::BodyType::from_program(program, &field.field_type),
                        &mut nominal_types,
                    );
                }
            }
        } else {
            return None;
        }
        type_facts.insert(identity.0, fingerprint);
    }

    let selected_functions = function_facts.keys().cloned().collect::<BTreeSet<_>>();
    let specializations = program
        .generic_specializations
        .iter()
        .filter(|row| selected_functions.contains(&row.generic_function.0))
        .map(serde_json::to_value)
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let identity = json!({
        "schema_version": "mncs.compiler.application-executable-closure-identity/1",
        "root_module": program.module,
        "callable_roots": callable_roots,
        "generic_seed_keys": generic_seed_keys,
        "function_semantic_facts": function_facts,
        "nominal_type_semantic_facts": type_facts,
        "generic_specializations": specializations,
        "program_assumptions": program.assumptions,
    });
    let mut bytes = b"mncs.compiler.application-executable-closure-identity/1\0".to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(&identity).ok()?);
    Some(format!("sha256:{}", mncs_model::sha256_hex(&bytes)))
}

fn rebind_cached_source_snapshot(
    mut response: Value,
    source_identity: &str,
    source_files: &[Value],
) -> Option<Value> {
    let snapshot = response.get_mut("source_snapshot")?.as_object_mut()?;
    snapshot.insert("identity".to_owned(), json!(source_identity));
    snapshot.insert("files".to_owned(), json!(source_files));
    Some(response)
}

fn rebind_cached_application_response(
    mut response: Value,
    source_identity: &str,
    source_files: &[Value],
    graph_identity: &str,
    graph_details: Value,
    semantic_identity: &str,
    executable_closure_identity: &str,
    entry_interface: Value,
    diagnostics: Value,
) -> Option<Value> {
    response = rebind_cached_source_snapshot(response, source_identity, source_files)?;
    let dependency_graph = response.get_mut("dependency_graph")?.as_object_mut()?;
    dependency_graph.insert("identity".to_owned(), json!(graph_identity));
    dependency_graph.insert("details".to_owned(), graph_details);
    dependency_graph.insert("semantic_identity".to_owned(), json!(semantic_identity));
    dependency_graph.insert(
        "application_executable_closure_identity".to_owned(),
        json!(executable_closure_identity),
    );
    response
        .get_mut("entry")?
        .as_object_mut()?
        .insert("interface".to_owned(), entry_interface);
    response
        .as_object_mut()?
        .insert("diagnostics".to_owned(), diagnostics);
    Some(response)
}

fn publish_application_cache(path: &PathBuf, key: &str, response: &Value) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "cache path has no parent".to_owned())?;
    fs::create_dir_all(parent).map_err(|error| format!("create cache directory: {error}"))?;
    let response_bytes =
        serde_json::to_vec(response).map_err(|error| format!("encode cached response: {error}"))?;
    let entry = json!({
        "schema_version": "mncs.compiler.application-build-cache-entry/3",
        "key": key,
        "response_sha256": format!("sha256:{}", mncs_model::sha256_hex(&response_bytes)),
        "response": response,
    });
    let encoded =
        serde_json::to_vec(&entry).map_err(|error| format!("encode cache entry: {error}"))?;
    let nonce = CACHE_NONCE.fetch_add(1, Ordering::Relaxed);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = parent.join(format!(
        ".{}.{}.{}.{}.tmp",
        key.trim_start_matches("sha256:"),
        std::process::id(),
        now,
        nonce
    ));
    let result = (|| -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|error| format!("create temporary cache entry: {error}"))?;
        file.write_all(&encoded)
            .map_err(|error| format!("write temporary cache entry: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("sync temporary cache entry: {error}"))?;
        fs::rename(&temp, path).map_err(|error| format!("publish cache entry: {error}"))?;
        if let Ok(directory) = OpenOptions::new().read(true).open(parent) {
            let _ = directory.sync_all();
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn publish_application_cache_index(
    path: &PathBuf,
    key: &str,
    candidate_key: &str,
) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "cache index path has no parent".to_owned())?;
    fs::create_dir_all(parent).map_err(|error| format!("create cache index directory: {error}"))?;
    let entry = json!({
        "schema_version": "mncs.compiler.application-build-cache-index/2",
        "key": key,
        "candidate_key": candidate_key,
    });
    let encoded =
        serde_json::to_vec(&entry).map_err(|error| format!("encode cache index: {error}"))?;
    let nonce = CACHE_NONCE.fetch_add(1, Ordering::Relaxed);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = parent.join(format!(
        ".{}.{}.{}.{}.tmp",
        key.trim_start_matches("sha256:"),
        std::process::id(),
        now,
        nonce
    ));
    let result = (|| -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temp)
            .map_err(|error| format!("create temporary cache index: {error}"))?;
        file.write_all(&encoded)
            .map_err(|error| format!("write temporary cache index: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("sync temporary cache index: {error}"))?;
        fs::rename(&temp, path).map_err(|error| format!("publish cache index: {error}"))?;
        if let Ok(directory) = OpenOptions::new().read(true).open(parent) {
            let _ = directory.sync_all();
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp);
    }
    result
}

fn enqueue_interface_type(type_name: &str, pending: &mut BTreeSet<String>) {
    let mut current = type_name.trim();
    loop {
        if let Some(inner) = current
            .strip_prefix('[')
            .and_then(|value| value.strip_suffix(']'))
        {
            if let Some((element, _bound)) = inner.rsplit_once(';') {
                current = element.trim();
                continue;
            }
        }
        if let Some(inner) = current
            .strip_prefix("vec<")
            .and_then(|value| value.strip_suffix('>'))
        {
            if let Some((element, _lanes)) = inner.rsplit_once(',') {
                current = element.trim();
                continue;
            }
        }
        pending.insert(current.to_owned());
        return;
    }
}

fn is_builtin_interface_type(type_name: &str) -> bool {
    if matches!(type_name, "bool" | "byte" | "unit" | "never")
        || type_name
            .strip_prefix("mask<")
            .and_then(|value| value.strip_suffix('>'))
            .is_some_and(|lanes| lanes.parse::<u32>().is_ok())
    {
        return true;
    }
    ["i", "u", "f"].iter().any(|prefix| {
        type_name.strip_prefix(prefix).is_some_and(|width| {
            !width.is_empty() && width.chars().all(|character| character.is_ascii_digit())
        })
    })
}

fn callable_interface(program: &mncs_model::Program, module: &str, name: &str) -> Option<Value> {
    let function = program.functions.iter().find(|function| {
        function.name == name && function.identity_namespace(&program.module) == module
    })?;
    let identity = mncs_model::function_id(module, name).0;
    let mut needed_types = BTreeSet::new();
    for value in function.inputs.iter().chain(function.outputs.iter()) {
        enqueue_interface_type(&value.value_type, &mut needed_types);
    }
    let mut visited_types = BTreeSet::new();
    let mut unresolved_types = BTreeSet::new();
    let mut record_types = Vec::new();
    let mut finite_types = Vec::new();
    while let Some(type_name) = needed_types.iter().next().cloned() {
        needed_types.remove(&type_name);
        if !visited_types.insert(type_name.clone()) {
            continue;
        }
        let matching_records: Vec<_> = program
            .record_types
            .iter()
            .filter(|record| record.name == type_name || record.identity.as_str() == type_name)
            .collect();
        let matching_finite: Vec<_> = program
            .finite_types
            .iter()
            .filter(|finite| finite.name == type_name || finite.identity.as_str() == type_name)
            .collect();
        if matching_records.len() + matching_finite.len() > 1 {
            unresolved_types.insert(format!("ambiguous type reference: {type_name}"));
        }
        for record in matching_records {
            for field in &record.fields {
                enqueue_interface_type(&field.field_type, &mut needed_types);
            }
            record_types.push(record);
        }
        for finite in matching_finite {
            for variant in &finite.variants {
                for field in &variant.payload {
                    enqueue_interface_type(&field.field_type, &mut needed_types);
                }
            }
            finite_types.push(finite);
        }
        if !record_types
            .iter()
            .any(|record| record.name == type_name || record.identity.as_str() == type_name)
            && !finite_types
                .iter()
                .any(|finite| finite.name == type_name || finite.identity.as_str() == type_name)
            && !is_builtin_interface_type(&type_name)
        {
            unresolved_types.insert(type_name);
        }
    }
    record_types.sort_by(|left, right| left.identity.0.cmp(&right.identity.0));
    finite_types.sort_by(|left, right| left.identity.0.cmp(&right.identity.0));
    let signature = json!({
        "function_identity": identity,
        "module": module,
        "name": name,
        "inputs": function.inputs,
        "outputs": function.outputs,
        "contracts": function.contracts,
        "effects": function.effects,
        "capabilities": function.capabilities,
        "failure": function.failure,
    });
    let types = json!({
        "status": if unresolved_types.is_empty() { "verified" } else { "unknown" },
        "unresolved_references": unresolved_types,
        "records": record_types,
        "finite": finite_types,
    });
    let interface = json!({ "signature": signature, "types": types });
    let signature_bytes = serde_json::to_vec(&interface).ok()?;
    Some(json!({
        "schema_version": "mncs.compiler.callable-interface/1",
        "status": interface["types"]["status"],
        "identity": format!("sha256:{}", mncs_model::sha256_hex(&signature_bytes)),
        "signature": interface["signature"],
        "types": interface["types"],
    }))
}

#[cfg(test)]
mod tests {
    use super::{
        application_executable_closure_identity, application_semantic_identity, callable_interface,
        envelope, ApplicationSources,
    };
    use mncs_compiler::ReferenceCompiler;
    use mncs_model::ArtifactRepresentation;
    use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
    use serde_json::json;
    use std::collections::BTreeMap;

    fn interface_program() -> mncs_model::Program {
        let source = r#"
            mncs 0.18;
            module contract.types;

            record Context {
                rows: [[byte; up_to 16]; up_to 4],
                label: [byte; 32]
            }

            record Output { code: u64 }

            fn entry(context: Context) -> (result: Output) {
                return Output { code: 0 };
            }
        "#;
        let result = ReferenceCompiler::default().front_end(SourceEnvelope::inline(
            SourceArtifactKind::Program,
            "application-interface-test",
            source,
        ));
        assert!(
            result.is_valid(),
            "interface test source: {:?}",
            result.diagnostics
        );
        result
            .program
            .expect("valid source should produce a Program")
    }

    #[test]
    fn callable_interface_resolves_nested_bounded_sequences() {
        let program = interface_program();
        let interface = callable_interface(&program, "contract.types", "entry")
            .expect("entry callable interface");

        assert_eq!(interface["status"], "verified");
        assert_eq!(interface["types"]["status"], "verified");
        assert_eq!(interface["types"]["unresolved_references"], json!([]));
        let records = interface["types"]["records"]
            .as_array()
            .expect("record type closure");
        assert_eq!(records.len(), 2);
        assert!(records.iter().any(|record| record["name"] == "Context"));
        assert!(records.iter().any(|record| record["name"] == "Output"));
    }

    #[test]
    fn callable_interface_keeps_unknown_nominal_types_unresolved() {
        let mut program = interface_program();
        let context = program
            .record_types
            .iter_mut()
            .find(|record| record.name == "Context")
            .expect("Context record");
        context
            .fields
            .iter_mut()
            .find(|field| field.name == "rows")
            .expect("rows field")
            .field_type = "MissingNominal".to_owned();

        let interface = callable_interface(&program, "contract.types", "entry")
            .expect("entry callable interface");

        assert_eq!(interface["status"], "unknown");
        assert_eq!(interface["types"]["status"], "unknown");
        assert_eq!(
            interface["types"]["unresolved_references"],
            json!(["MissingNominal"])
        );
    }

    #[test]
    fn application_semantic_identity_ignores_source_identity_but_binds_module_semantics() {
        let root = r#"
            mncs 0.18;
            module semantic.root;
            use semantic.dep as dep;
            fn entry() -> (result: u64) { return dep.value(); }
        "#;
        let dependency = r#"
            mncs 0.18;
            module semantic.dep;
            fn value() -> (result: u64) { return 7; }
        "#;
        let resolver = ApplicationSources(BTreeMap::from([(
            "semantic.dep".to_owned(),
            envelope(dependency),
        )]));
        let frontend = ReferenceCompiler::default().front_end_with_resolver_and_seeds(
            envelope(root),
            &resolver,
            &[],
        );
        assert!(
            frontend.is_valid(),
            "semantic identity source: {:?}",
            frontend.diagnostics
        );
        let program = frontend.program.as_ref().expect("valid root Program");
        let root_fingerprint = &frontend
            .artifact(ArtifactRepresentation::Semantic)
            .expect("canonical root semantic artifact")
            .fingerprint;
        let baseline = application_semantic_identity(
            program,
            root_fingerprint,
            &frontend.module_resolutions,
            &[],
        )
        .expect("complete semantic identity");

        let mut source_only_change = frontend.module_resolutions.clone();
        source_only_change[0]
            .source_identity
            .push_str(":comment-edit");
        let source_only_identity =
            application_semantic_identity(program, root_fingerprint, &source_only_change, &[])
                .expect("source identity is not a semantic input");
        assert_eq!(baseline, source_only_identity);

        let mut semantic_change = frontend.module_resolutions.clone();
        semantic_change[0].semantic_fingerprint = Some("changed-semantic-fingerprint".to_owned());
        let changed_identity =
            application_semantic_identity(program, root_fingerprint, &semantic_change, &[])
                .expect("changed dependency semantic fingerprint");
        assert_ne!(baseline, changed_identity);

        let mut incomplete = frontend.module_resolutions.clone();
        incomplete[0].semantic_fingerprint = None;
        assert!(
            application_semantic_identity(program, root_fingerprint, &incomplete, &[]).is_none()
        );
    }

    #[test]
    fn executable_closure_identity_reuses_unreachable_body_edits_only() {
        let root = r#"
            mncs 0.18;
            module semantic.root;
            use semantic.dep as dep;
            fn entry() -> (result: u64) { return dep.value(); }
        "#;
        let dependency = r#"
            mncs 0.18;
            module semantic.dep;
            fn value() -> (result: u64) { return 7; }
            fn shrink(input: u64) -> (result: u64) { return input / 2; }
        "#;
        let compile_facts = |dependency_source: &str| {
            let resolver = ApplicationSources(BTreeMap::from([(
                "semantic.dep".to_owned(),
                envelope(dependency_source),
            )]));
            let frontend = ReferenceCompiler::default().front_end_with_resolver_and_seeds(
                envelope(root),
                &resolver,
                &[],
            );
            assert!(
                frontend.is_valid(),
                "closure identity source: {:?}",
                frontend.diagnostics
            );
            let program = frontend.program.as_ref().expect("valid root Program");
            let closure_identity =
                application_executable_closure_identity(program, "semantic.root", "entry", &[])
                    .expect("verified executable closure identity");
            let full_semantic_identity = application_semantic_identity(
                program,
                &frontend
                    .artifact(ArtifactRepresentation::Semantic)
                    .expect("canonical root semantic artifact")
                    .fingerprint,
                &frontend.module_resolutions,
                &[],
            )
            .expect("complete source semantic identity");
            (closure_identity, full_semantic_identity)
        };

        let baseline = compile_facts(dependency);
        let unrelated_function_edit =
            compile_facts(&dependency.replace("return input / 2", "return input / 3"));
        assert_ne!(baseline.1, unrelated_function_edit.1);
        assert_eq!(baseline.0, unrelated_function_edit.0);

        let reachable_function_edit = compile_facts(&dependency.replace("return 7", "return 8"));
        assert_ne!(baseline.0, reachable_function_edit.0);
    }
}

/// Compile a bounded module snapshot with the pinned compiler and emit the
/// existing canonical VM artifact format. Inputs are explicit and immutable;
/// source identities are derived from exact bytes, never paths or timestamps.
pub(crate) fn compile_application(request: &Value, toolchain: Option<&ToolchainIdentity>) -> Value {
    if request.get("schema_version").and_then(Value::as_str) != Some(REQUEST) {
        return error_contract(
            "unknown",
            "MCA100",
            "unsupported application build request schema",
        );
    }
    if request.get("target").and_then(Value::as_str) != Some(VM_TARGET) {
        return error_contract(
            "unavailable",
            "MCA101",
            "requested execution target is unavailable",
        );
    }
    let generic_seeds: Vec<HostGenericSeedRequest> = match request.get("generic_seeds") {
        None => Vec::new(),
        Some(value) => {
            let Some(rows) = value.as_array() else {
                return error_contract(
                    "unknown",
                    "MCA109",
                    "generic_seeds must be an array of specialization requests",
                );
            };
            if rows.len() > MAX_GENERIC_SEEDS {
                return error_contract(
                    "unsupported",
                    "MCA110",
                    "generic specialization count exceeds the bounded application input",
                );
            }
            match parse_generic_seeds(value) {
                Ok(seeds) => seeds,
                Err(()) => {
                    return error_contract(
                        "unknown",
                        "MCA109",
                        "generic_seeds contains a malformed specialization request",
                    )
                }
            }
        }
    };
    let Some(root_id) = request.get("root_source_id").and_then(Value::as_str) else {
        return error_contract("unknown", "MCA102", "root_source_id is required");
    };
    let Some(root_source) = request.get("root_source").and_then(Value::as_str) else {
        return error_contract("unknown", "MCA103", "root_source is required");
    };
    let Some(entry) = request.get("entry").and_then(Value::as_object) else {
        return error_contract(
            "unknown",
            "MCA104",
            "entry module and function are required",
        );
    };
    let Some(entry_module) = entry.get("module").and_then(Value::as_str) else {
        return error_contract(
            "unknown",
            "MCA104",
            "entry module and function are required",
        );
    };
    let Some(entry_function) = entry.get("function").and_then(Value::as_str) else {
        return error_contract(
            "unknown",
            "MCA104",
            "entry module and function are required",
        );
    };
    let Some(raw_modules) = request.get("modules").and_then(Value::as_object) else {
        return error_contract(
            "unknown",
            "MCA105",
            "modules must be a module-to-source object",
        );
    };
    if raw_modules.len() > MAX_MODULES {
        return error_contract(
            "unsupported",
            "MCA106",
            "module count exceeds the bounded application input",
        );
    }

    let mut modules = BTreeMap::new();
    let mut total_bytes = root_source.len();
    for (module, value) in raw_modules {
        let Some(source) = value.as_str() else {
            return error_contract("unknown", "MCA107", "module source values must be strings");
        };
        total_bytes = total_bytes
            .saturating_add(module.len())
            .saturating_add(source.len());
        modules.insert(module.clone(), source.to_owned());
    }
    if total_bytes > MAX_SOURCE_BYTES {
        return error_contract(
            "unsupported",
            "MCA108",
            "source snapshot exceeds the bounded application input",
        );
    }

    let generic_seed_functions: BTreeSet<(String, String)> = request
        .get("generic_seeds")
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|row| {
                    Some((
                        row.get("module")?.as_str()?.to_owned(),
                        row.get("function")?.as_str()?.to_owned(),
                    ))
                })
                .collect()
        })
        .unwrap_or_default();

    let source_identity = source_snapshot_identity(root_id, root_source, &modules);
    let source_files: Vec<Value> = std::iter::once(json!({
        "module": "root",
        "source_id": root_id,
        "sha256": format!("sha256:{}", mncs_model::sha256_hex(root_source.as_bytes())),
    }))
    .chain(modules.iter().map(|(module, source)| {
        json!({
            "module": module,
            "source_id": module,
            "sha256": format!("sha256:{}", mncs_model::sha256_hex(source.as_bytes())),
        })
    }))
    .collect();

    let (probe_identity, probe_details) = producer_identity();
    let cache_key = application_cache_key(
        request,
        root_id,
        &source_identity,
        &modules,
        &generic_seeds,
        toolchain,
        &probe_identity,
    );
    let cache_index_key = application_cache_index_key(
        request,
        root_id,
        root_source,
        &generic_seeds,
        toolchain,
        &probe_identity,
    );
    let cache_path = application_cache_path(&cache_key);
    let cache_index_path = application_cache_index_path(&cache_index_key);
    if let Some(path) = cache_path.as_ref() {
        if let Some(response) = read_application_cache(
            path,
            &cache_key,
            Some(&source_identity),
            &probe_identity,
            toolchain,
        ) {
            return annotate_cache(
                response,
                &cache_key,
                "hit",
                "previously_published",
                &["verified_application_artifact"],
                &[],
            );
        }
    }
    let mut semantic_cache_candidate: Option<(Value, String)> = None;
    if let (Some(index_path), Some(_)) = (cache_index_path.as_ref(), cache_path.as_ref()) {
        if let Some(candidate_key) = read_application_cache_index(index_path, &cache_index_key) {
            if let Some(candidate_path) = application_cache_path(&candidate_key) {
                if let Some(candidate) = read_application_cache(
                    &candidate_path,
                    &candidate_key,
                    None,
                    &probe_identity,
                    toolchain,
                ) {
                    if cached_application_matches_dependency_closure(
                        &candidate,
                        root_id,
                        root_source,
                        &modules,
                    ) {
                        if let Some(response) = rebind_cached_source_snapshot(
                            candidate.clone(),
                            &source_identity,
                            &source_files,
                        ) {
                            return annotate_cache_with_basis(
                                response,
                                &cache_key,
                                "hit",
                                "previously_published",
                                "verified_dependency_closure",
                                Some(&candidate_key),
                                &["verified_application_artifact"],
                                &["request_source_snapshot_identity"],
                            );
                        }
                    }
                    semantic_cache_candidate = Some((candidate, candidate_key));
                }
            }
        }
    }

    let resolver = ApplicationSources(
        modules
            .iter()
            .map(|(module, source)| (module.clone(), envelope(source)))
            .collect(),
    );
    let compiler = ReferenceCompiler::default();
    let frontend = compiler.front_end_with_resolver_and_seeds(
        envelope(root_source),
        &resolver,
        &generic_seeds,
    );
    let diagnostics = serde_json::to_value(&frontend.diagnostics).unwrap_or_else(|_| json!([]));
    let resolutions =
        serde_json::to_value(&frontend.module_resolutions).unwrap_or_else(|_| json!([]));
    let Some(program) = frontend.program.as_ref().filter(|_| frontend.is_valid()) else {
        return annotate_cache(
            json!({
                "schema_version": CONTRACT,
                "status": "rejected",
                "source_snapshot": {
                    "identity": source_identity,
                    "files": source_files,
                },
                "dependency_graph": {
                    "identity": Value::Null,
                    "resolutions": resolutions,
                },
                "producer": {
                    "stage0_toolchain": toolchain
                        .and_then(|identity| serde_json::to_value(identity).ok()),
                    "probe": probe_details,
                },
                "entry": { "module": entry_module, "function": entry_function },
                "artifact": null,
                "capability_dispatch": {
                    "schema_version": CAPABILITY_DISPATCH_SCHEMA,
                    "status": "unknown",
                    "bindings": [],
                    "unresolved_obligations": [],
                },
                "diagnostics": diagnostics,
                "unresolved_obligations": [],
                "unselected_unresolved_exports": [],
            }),
            &cache_key,
            "bypass",
            "not_published",
            &[],
            &["source_semantic_check"],
        );
    };

    let entry_interface = callable_interface(program, entry_module, entry_function);
    let semantic_identity = frontend
        .artifact(ArtifactRepresentation::Semantic)
        .and_then(|artifact| {
            application_semantic_identity(
                program,
                &artifact.fingerprint,
                &frontend.module_resolutions,
                &generic_seeds,
            )
        });
    let executable_closure_identity = application_executable_closure_identity(
        program,
        entry_module,
        entry_function,
        &generic_seeds,
    );

    let graph_value = json!({
        "root_module": program.module,
        "dependencies": program.dependencies,
        "resolutions": resolutions,
    });
    let graph_bytes = serde_json::to_vec(&graph_value).unwrap_or_default();
    let graph_identity = format!("sha256:{}", mncs_model::sha256_hex(&graph_bytes));
    if let (
        Some((candidate, candidate_key)),
        Some(semantic_identity),
        Some(executable_closure_identity),
        Some(interface),
    ) = (
        semantic_cache_candidate,
        semantic_identity.as_deref(),
        executable_closure_identity.as_deref(),
        entry_interface.clone(),
    ) {
        let current_interface_identity = interface.get("identity").and_then(Value::as_str);
        let cached_interface_identity = candidate
            .pointer("/entry/interface/identity")
            .and_then(Value::as_str);
        let executable_closure_match = candidate
            .pointer("/dependency_graph/application_executable_closure_identity")
            .and_then(Value::as_str)
            == Some(executable_closure_identity);
        let current_artifact_interface_identity =
            mncs_codegen::language_owned_interface_identity(program);
        let cached_artifact_interface_identity = candidate
            .pointer("/artifact/value/source/interface_identity")
            .and_then(Value::as_str);
        if candidate.get("status").and_then(Value::as_str) == Some("verified")
            && executable_closure_match
            && interface.get("status").and_then(Value::as_str) == Some("verified")
            && current_interface_identity.is_some()
            && current_interface_identity == cached_interface_identity
            && cached_artifact_interface_identity
                == Some(current_artifact_interface_identity.as_str())
        {
            if let Some(response) = rebind_cached_application_response(
                candidate,
                &source_identity,
                &source_files,
                &graph_identity,
                graph_value.clone(),
                semantic_identity,
                executable_closure_identity,
                interface,
                diagnostics.clone(),
            ) {
                let publication = match cache_path.as_ref() {
                    Some(path) => match publish_application_cache(path, &cache_key, &response) {
                        Ok(()) => {
                            if let Some(index_path) = cache_index_path.as_ref() {
                                let _ = publish_application_cache_index(
                                    index_path,
                                    &cache_index_key,
                                    &cache_key,
                                );
                            }
                            "stored"
                        }
                        Err(_) => "write_failed",
                    },
                    None => "not_configured",
                };
                return annotate_cache_with_basis(
                    response,
                    &cache_key,
                    "hit",
                    publication,
                    "verified_application_executable_closure",
                    Some(&candidate_key),
                    &["verified_vm_artifact"],
                    &[
                        "source_semantic_check",
                        "application_executable_closure_verification",
                    ],
                );
            }
        }
    }
    let producer_reference = format!("stage0-probe:{probe_identity}");
    let (artifact, unselected_unresolved, capability_bindings, emission_error) =
        match vm_emit::emit_vm_application_artifact_with_producer(
            &compiler,
            program,
            Some(&producer_reference),
            entry_module,
            entry_function,
            &generic_seed_functions,
        ) {
            Ok((artifact, unselected_unresolved, capability_bindings)) => (
                Some(artifact),
                unselected_unresolved,
                capability_bindings,
                None,
            ),
            Err(error) => (None, Vec::new(), Vec::new(), Some(error)),
        };

    let Some(artifact) = artifact else {
        let obligation = emission_error.unwrap_or_else(|| "artifact emission failed".to_owned());
        return annotate_cache(
            json!({
                "schema_version": CONTRACT,
                "status": "unsupported",
                "source_snapshot": { "identity": source_identity, "files": source_files },
                "dependency_graph": { "identity": graph_identity, "details": graph_value },
                "producer": {
                    "stage0_toolchain": toolchain
                        .and_then(|identity| serde_json::to_value(identity).ok()),
                    "probe": probe_details,
                },
                "entry": { "module": entry_module, "function": entry_function },
                "artifact": null,
                "capability_dispatch": {
                    "schema_version": CAPABILITY_DISPATCH_SCHEMA,
                    "status": "unsupported",
                    "bindings": [],
                    "unresolved_obligations": [obligation],
                },
                "diagnostics": diagnostics,
                "unresolved_obligations": [obligation],
                "unselected_unresolved_exports": unselected_unresolved,
            }),
            &cache_key,
            "bypass",
            "not_published",
            &[],
            &["verified_vm_artifact"],
        );
    };

    let raw_unsupported = artifact
        .get("unsupported")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let entry_available = artifact
        .get("callables")
        .and_then(Value::as_array)
        .is_some_and(|rows| {
            rows.iter().any(|row| {
                row.get("module").and_then(Value::as_str) == Some(entry_module)
                    && row.get("name").and_then(Value::as_str) == Some(entry_function)
            })
        });
    let target = artifact
        .pointer("/source/target")
        .cloned()
        .unwrap_or(Value::Null);
    let interface_identity = artifact
        .pointer("/source/interface_identity")
        .cloned()
        .unwrap_or(Value::Null);
    let artifact_identity = artifact.get("artifact_id").cloned().unwrap_or(Value::Null);
    let capabilities = artifact
        .pointer("/requirements/capabilities")
        .cloned()
        .unwrap_or_else(|| json!([]));
    let vm_contract = artifact
        .pointer("/requirements/vm_contract")
        .cloned()
        .unwrap_or(Value::Null);
    let mut unresolved = raw_unsupported;
    if !entry_available {
        unresolved.push(json!(format!(
            "entry {}::{} has no admitted callable",
            entry_module, entry_function
        )));
    }
    let interface_verified = entry_interface
        .as_ref()
        .and_then(|interface| interface.get("status"))
        .and_then(Value::as_str)
        == Some("verified");
    let entry_status = if !entry_available {
        "unsupported"
    } else if toolchain.is_none() || probe_identity == "unknown" || !interface_verified {
        "unknown"
    } else {
        "verified"
    };
    let status = if !unresolved.is_empty() {
        "unsupported"
    } else if entry_status == "unknown" {
        "unknown"
    } else {
        "verified"
    };

    let capability_dispatch = json!({
        "schema_version": CAPABILITY_DISPATCH_SCHEMA,
        "status": "verified",
        "bindings": capability_bindings,
        "unresolved_obligations": [],
    });
    let response = json!({
        "schema_version": CONTRACT,
        "status": status,
        "source_snapshot": {
            "identity": source_identity,
            "files": source_files,
        },
        "dependency_graph": {
            "identity": graph_identity,
            "semantic_identity": semantic_identity,
            "application_executable_closure_identity": executable_closure_identity,
            "details": graph_value,
        },
        "producer": {
            "stage0_toolchain": toolchain
                .and_then(|identity| serde_json::to_value(identity).ok()),
            "probe": probe_details,
            "artifact_backend": artifact.pointer("/source/backend_name").cloned().unwrap_or(Value::Null),
        },
        "build_assumptions": {
            "backend": "mncs-vm-direct",
            "target": request.get("target").cloned().unwrap_or(Value::Null),
            "capabilities": request.get("capabilities").cloned().unwrap_or_else(|| json!([])),
            "effect_assumptions": request.get("effect_assumptions").cloned().unwrap_or_else(|| json!([])),
            "generic_seeds": serde_json::to_value(&generic_seeds).unwrap_or_else(|_| json!([])),
            "optimization_policy": request.get("optimization_policy").cloned().unwrap_or_else(|| json!("default")),
            "verification_policy": request.get("verification_policy").cloned().unwrap_or_else(|| json!("default")),
        },
        "callable_scope": {
            "kind": "entry_and_explicit_generic_seeds",
            "entry": {
                "module": entry_module,
                "function": entry_function,
            },
            "generic_seed_functions": generic_seed_functions,
        },
        "artifact": {
            "schema_version": artifact.get("schema_version").cloned().unwrap_or(Value::Null),
            "identity": artifact_identity,
            "vm_contract": vm_contract,
            "target": target,
            "interface_identity": interface_identity,
            "required_capabilities": capabilities,
            "unresolved_obligations": unresolved,
            "value": artifact,
        },
        "capability_dispatch": capability_dispatch,
        "entry": {
            "module": entry_module,
            "function": entry_function,
            "status": entry_status,
            "interface": entry_interface,
        },
        "execution_target_compatibility": {
            "target": VM_TARGET,
            "status": entry_status,
            "compatible": entry_status == "verified",
            "execution_status": "not_run",
        },
        "resource_intent": {
            "compile_cpu_ms": "unknown",
            "compile_peak_memory_bytes": "unknown",
            "execution_step_budget": "unknown",
            "uncertainty": "this producer contract records no resource estimate; measure the admitted Forge/VM execution",
        },
        "diagnostics": diagnostics,
        "unresolved_obligations": unresolved,
        "unselected_unresolved_exports": unselected_unresolved,
    });
    let (cache_status, publication) = match cache_path.as_ref() {
        Some(path) if entry_status == "verified" => {
            match publish_application_cache(path, &cache_key, &response) {
                Ok(()) => {
                    if let Some(index_path) = cache_index_path.as_ref() {
                        let _ = publish_application_cache_index(
                            index_path,
                            &cache_index_key,
                            &cache_key,
                        );
                    }
                    ("miss", "stored")
                }
                Err(_) => ("miss", "write_failed"),
            }
        }
        Some(_) => ("bypass", "not_published"),
        None => ("disabled", "not_configured"),
    };
    annotate_cache(
        response,
        &cache_key,
        cache_status,
        publication,
        &[],
        &[
            "source_semantic_check",
            "ssa_verification",
            "verified_vm_artifact",
        ],
    )
}
