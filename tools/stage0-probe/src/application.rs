//! Versioned source-snapshot application build contract for the canonical VM.
//!
//! This is an adapter around the pinned Stage-0 compiler and the existing
//! compiler-owned mncs.vm.artifact/1 emitter. The application contract
//! records provenance and obligations; it does not replace the VM artifact as
//! the executable authority or implement language semantics here.
use crate::{vm_emit, ToolchainIdentity};
use mncs_compiler::{ModuleResolver, ReferenceCompiler};
use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

static CACHE_NONCE: AtomicU64 = AtomicU64::new(0);
static PRODUCER_IDENTITY: OnceLock<(String, Value)> = OnceLock::new();

const CONTRACT: &str = "mncs.compiler.application-build/1";
const REQUEST: &str = "mncs.compiler.application-build-request/1";
const VM_TARGET: &str = "mncs-vm-0.1";
const MAX_MODULES: usize = 64;
const MAX_SOURCE_BYTES: usize = 4 * 1024 * 1024;

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
        "diagnostics": [],
        "unresolved_obligations": [],
    })
}

fn application_cache_key(
    request: &Value,
    root_id: &str,
    source_identity: &str,
    modules: &BTreeMap<String, String>,
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
    let identity = json!({
        "schema_version": "mncs.compiler.application-build-cache-key/1",
        "contract": CONTRACT,
        "source_snapshot_identity": source_identity,
        "root_source_id": root_id,
        "module_sources": module_sources,
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
    let mut bytes = b"mncs.compiler.application-build-cache/1\0".to_vec();
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

fn annotate_cache(
    mut response: Value,
    key: &str,
    status: &str,
    publication: &str,
    reused_units: &[&str],
    recomputed_units: &[&str],
) -> Value {
    if let Some(object) = response.as_object_mut() {
        object.insert(
            "cache".to_owned(),
            json!({
                "schema_version": "mncs.compiler.application-build-cache-observation/1",
                "key": key,
                "status": status,
                "publication": publication,
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
    source_identity: &str,
    probe_identity: &str,
    toolchain: Option<&ToolchainIdentity>,
) -> Option<Value> {
    let entry: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if entry.get("schema_version").and_then(Value::as_str)
        != Some("mncs.compiler.application-build-cache-entry/1")
        || entry.get("key").and_then(Value::as_str) != Some(key)
    {
        return None;
    }
    let response = entry.get("response")?.clone();
    let response_bytes = serde_json::to_vec(&response).ok()?;
    let response_digest = format!("sha256:{}", mncs_model::sha256_hex(&response_bytes));
    if entry.get("response_sha256").and_then(Value::as_str) != Some(response_digest.as_str())
        || response
            .pointer("/source_snapshot/identity")
            .and_then(Value::as_str)
            != Some(source_identity)
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

fn publish_application_cache(path: &PathBuf, key: &str, response: &Value) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "cache path has no parent".to_owned())?;
    fs::create_dir_all(parent).map_err(|error| format!("create cache directory: {error}"))?;
    let response_bytes =
        serde_json::to_vec(response).map_err(|error| format!("encode cached response: {error}"))?;
    let entry = json!({
        "schema_version": "mncs.compiler.application-build-cache-entry/1",
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
        toolchain,
        &probe_identity,
    );
    let cache_path = application_cache_path(&cache_key);
    if let Some(path) = cache_path.as_ref() {
        if let Some(response) = read_application_cache(
            path,
            &cache_key,
            &source_identity,
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

    let resolver = ApplicationSources(
        modules
            .iter()
            .map(|(module, source)| (module.clone(), envelope(source)))
            .collect(),
    );
    let compiler = ReferenceCompiler::default();
    let frontend = compiler.front_end_with_resolver(envelope(root_source), &resolver);
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
                "diagnostics": diagnostics,
                "unresolved_obligations": [],
            }),
            &cache_key,
            "bypass",
            "not_published",
            &[],
            &["source_semantic_check"],
        );
    };

    let graph_value = json!({
        "root_module": program.module,
        "dependencies": program.dependencies,
        "resolutions": resolutions,
    });
    let graph_bytes = serde_json::to_vec(&graph_value).unwrap_or_default();
    let graph_identity = format!("sha256:{}", mncs_model::sha256_hex(&graph_bytes));
    let producer_reference = format!("stage0-probe:{probe_identity}");
    let (artifact, emission_error) = match vm_emit::emit_vm_artifact_with_producer(
        &compiler,
        program,
        Some(&producer_reference),
    ) {
        Ok(artifact) => (Some(artifact), None),
        Err(error) => (None, Some(error)),
    };

    let Some(artifact) = artifact else {
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
                "diagnostics": diagnostics,
                "unresolved_obligations": [emission_error.unwrap_or_else(|| "artifact emission failed".to_owned())],
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
    let status = if !unresolved.is_empty() {
        "unsupported"
    } else if toolchain.is_none() || probe_identity == "unknown" {
        "unknown"
    } else {
        "verified"
    };
    let entry_status = if !entry_available {
        "unsupported"
    } else if toolchain.is_none() || probe_identity == "unknown" {
        "unknown"
    } else {
        "verified"
    };

    let response = json!({
        "schema_version": CONTRACT,
        "status": status,
        "source_snapshot": {
            "identity": source_identity,
            "files": source_files,
        },
        "dependency_graph": {
            "identity": graph_identity,
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
            "optimization_policy": request.get("optimization_policy").cloned().unwrap_or_else(|| json!("default")),
            "verification_policy": request.get("verification_policy").cloned().unwrap_or_else(|| json!("default")),
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
        "entry": {
            "module": entry_module,
            "function": entry_function,
            "status": entry_status,
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
    });
    let (cache_status, publication) = match cache_path.as_ref() {
        Some(path) if entry_status == "verified" => {
            match publish_application_cache(path, &cache_key, &response) {
                Ok(()) => ("miss", "stored"),
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
