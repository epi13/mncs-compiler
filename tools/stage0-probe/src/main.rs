//! Temporary test transport only. All compiler behavior comes from the pinned
//! reference libraries or the MNCS source under test. No replacement semantics.
use mncs_codegen::{configuration_for_backend, target_for_backend, OwnedExecutionSession};
use mncs_compiler::{
    native_host_identities, ModuleResolver, NullResolver, ReferenceCompiler,
    REFERENCE_LANGUAGE_PROFILE,
};
use mncs_model::{
    ArtifactRepresentation, BackendConfiguration, BodyExecutionSession, BuildHostIdentity,
    CompilerHostIdentity, CompilerImplementationIdentity, ExecutionRequest, HostGenericSeedRequest,
    SemanticId, TargetContractRef,
};
use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, BufRead},
};

#[path = "../../vm_emit.rs"]
mod vm_emit;
mod provider;

struct Sources(BTreeMap<String, SourceEnvelope>);
impl ModuleResolver for Sources {
    fn resolve(&self, name: &str) -> Option<SourceEnvelope> {
        self.0.get(name).cloned()
    }
}
fn envelope(text: String) -> SourceEnvelope {
    SourceEnvelope::inline(SourceArtifactKind::Program, "probe", text)
}

/// Cache key schema. Bump whenever cache read/write logic changes: a
/// logic change without a bump could otherwise misread old entries.
const PROBE_CACHE_SCHEMA: &str = "probe-cache-v2";
const TOOLCHAIN_IDENTITY_SCHEMA: &str = "probe-toolchain/1";
const UNKNOWN_IDENTITY_PART: &str = "unknown";

/// Every identity capable of changing probe elaboration, lowering, or
/// artifact semantics. Bound into the cache key and echoed into each
/// entry, so a Stage-0 repin, backend/adapter change, host change, or
/// toolchain change is a miss, never a false hit. A repin produces
/// misses exactly where these identities changed; entries whose stored
/// identity still matches remain reusable.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
struct ToolchainIdentity {
    schema: String,
    /// Declared Stage-0 pin from mncs-language.lock.json.
    lock_revision: String,
    lock_source_profile: String,
    /// Actually provisioned Stage-0 revision from .bootstrap/revision.
    provisioned_revision: String,
    /// Exact digest of the provisioned Stage-0 source tree (the crates
    /// this probe links, plus embedded files such as the stdlib bundle).
    bootstrap_tree_sha256: String,
    /// Exact dependency versions (including the Cranelift release).
    bootstrap_cargo_lock_sha256: String,
    /// The reference driver identities used for elaboration/lowering.
    compiler_implementation: CompilerImplementationIdentity,
    pipeline_identity: SemanticId,
    language_profile: String,
    /// Backend name, full adapter configuration, and target contract.
    backend_name: Option<String>,
    backend_configuration: Option<BackendConfiguration>,
    target: Option<TargetContractRef>,
    /// Host identities (native artifacts are host-sensitive) and the
    /// actual rustc that built this probe binary's toolchain.
    compiler_host: CompilerHostIdentity,
    build_host: BuildHostIdentity,
    rustc_version: String,
}

fn gather_toolchain_identity(
    backend_name: Option<&str>,
    lock_path: &std::path::Path,
    bootstrap_dir: &std::path::Path,
) -> ToolchainIdentity {
    let (lock_revision, lock_source_profile) = read_lock_pin(lock_path);
    let provisioned_revision = read_trimmed_file(&bootstrap_dir.join("revision"))
        .unwrap_or_else(|| UNKNOWN_IDENTITY_PART.to_owned());
    let bootstrap_tree_sha256 = hash_tree(bootstrap_dir);
    let bootstrap_cargo_lock_sha256 = hash_optional_file(&bootstrap_dir.join("Cargo.lock"));
    let compiler = ReferenceCompiler::default();
    let (compiler_host, build_host) = native_host_identities();
    let rustc_version = std::process::Command::new("rustc")
        .arg("--version")
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_owned())
        .filter(|version| !version.is_empty())
        .unwrap_or_else(|| UNKNOWN_IDENTITY_PART.to_owned());
    ToolchainIdentity {
        schema: TOOLCHAIN_IDENTITY_SCHEMA.to_owned(),
        lock_revision,
        lock_source_profile,
        provisioned_revision,
        bootstrap_tree_sha256,
        bootstrap_cargo_lock_sha256,
        compiler_implementation: compiler.identity.clone(),
        pipeline_identity: compiler.pipeline.identity.clone(),
        language_profile: REFERENCE_LANGUAGE_PROFILE.to_owned(),
        backend_name: backend_name.map(str::to_owned),
        backend_configuration: backend_name.and_then(configuration_for_backend),
        target: backend_name.and_then(target_for_backend),
        compiler_host,
        build_host,
        rustc_version,
    }
}

fn read_lock_pin(lock_path: &std::path::Path) -> (String, String) {
    let unknown = || {
        (
            UNKNOWN_IDENTITY_PART.to_owned(),
            UNKNOWN_IDENTITY_PART.to_owned(),
        )
    };
    let raw = match std::fs::read_to_string(lock_path) {
        Ok(raw) => raw,
        Err(_) => return unknown(),
    };
    let lock: serde_json::Value = match serde_json::from_str(&raw) {
        Ok(lock) => lock,
        Err(_) => return unknown(),
    };
    let revision = lock
        .get("revision")
        .and_then(serde_json::Value::as_str)
        .unwrap_or(UNKNOWN_IDENTITY_PART);
    let profile = lock
        .get("source_profile")
        .and_then(serde_json::Value::as_str)
        .unwrap_or(UNKNOWN_IDENTITY_PART);
    (revision.to_owned(), profile.to_owned())
}

fn read_trimmed_file(path: &std::path::Path) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|text| text.trim().to_owned())
        .filter(|text| !text.is_empty())
}

fn hash_optional_file(path: &std::path::Path) -> String {
    std::fs::read(path)
        .map(|bytes| mncs_model::sha256_hex(&bytes))
        .unwrap_or_else(|_| UNKNOWN_IDENTITY_PART.to_owned())
}

/// Exact digest of the provisioned Stage-0 tree: every file's relative
/// path and bytes, sorted, excluding build outputs (`target/`). Any
/// semantic change in the linked tree changes the key.
fn hash_tree(root: &std::path::Path) -> String {
    fn visit(
        dir: &std::path::Path,
        root: &std::path::Path,
        files: &mut Vec<(String, Vec<u8>)>,
    ) -> bool {
        let entries = match std::fs::read_dir(dir) {
            Ok(entries) => entries,
            Err(_) => return false,
        };
        let mut entries: Vec<_> = entries.filter_map(|entry| entry.ok()).collect();
        entries.sort_by(|a, b| a.file_name().cmp(&b.file_name()));
        for entry in entries {
            if entry.file_name() == "target" {
                continue;
            }
            let path = entry.path();
            let rel = path
                .strip_prefix(root)
                .unwrap_or(&path)
                .to_string_lossy()
                .replace('\\', "/");
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => {
                    if !visit(&path, root, files) {
                        return false;
                    }
                }
                Ok(kind) if kind.is_file() => match std::fs::read(&path) {
                    Ok(bytes) => files.push((rel, bytes)),
                    Err(_) => return false,
                },
                _ => match std::fs::read_link(&path) {
                    Ok(target) => files.push((rel, target.to_string_lossy().as_bytes().to_vec())),
                    Err(_) => return false,
                },
            }
        }
        true
    }
    let mut files = Vec::new();
    if !visit(root, root, &mut files) {
        return UNKNOWN_IDENTITY_PART.to_owned();
    }
    let mut buffer = Vec::new();
    for (rel, bytes) in &files {
        buffer.extend_from_slice(rel.as_bytes());
        buffer.push(0);
        buffer.extend_from_slice(bytes);
        buffer.push(0);
    }
    mncs_model::sha256_hex(&buffer)
}

fn cache_key_hex(
    toolchain: &ToolchainIdentity,
    module: &str,
    closure: &[(String, String)],
    seeds_raw: &str,
) -> String {
    let mut input = String::from(PROBE_CACHE_SCHEMA);
    input.push('\n');
    input.push_str(&serde_json::to_string(toolchain).expect("toolchain identity serializes"));
    input.push('\n');
    input.push_str(module);
    input.push('\n');
    for (name, bytes) in closure {
        input.push_str(name);
        input.push('\n');
        input.push_str(bytes);
        input.push('\n');
    }
    input.push_str(seeds_raw);
    mncs_model::sha256_hex(input.as_bytes())
}

/// Parse one decompressed cache entry. Every failure mode is a safe
/// miss (`None`): corrupt bytes, missing fields, a stored toolchain
/// identity that differs from the current one, or an artifact whose own
/// identity does not validate.
fn parse_cache_entry(
    raw: &[u8],
    expected: &ToolchainIdentity,
    backend_name: Option<&str>,
) -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
    let entry: serde_json::Value = serde_json::from_slice(raw).ok()?;
    let stored: ToolchainIdentity = serde_json::from_value(entry.get("identity")?.clone()).ok()?;
    if &stored != expected {
        return None;
    }
    let program =
        serde_json::from_value::<mncs_model::Program>(entry.get("program")?.clone()).ok()?;
    if backend_name.is_none() {
        return Some((program, None));
    }
    let artifact =
        serde_json::from_value::<mncs_model::BackendArtifact>(entry.get("artifact")?.clone())
            .ok()?;
    if !artifact.identity_is_valid() {
        return None;
    }
    Some((program, Some(artifact)))
}

fn short_hash(value: &str) -> &str {
    &value[..value.len().min(12)]
}
fn main() {
    if std::env::args().any(|arg| arg == "--producer-info") {
        println!("{}", provider::producer());
        return;
    }
    if let Some(path) = std::env::args().find_map(|arg| arg.strip_prefix("--provider-request=").map(str::to_owned)) {
        match provider::run(&path) {
            Ok(value) => println!("{}", value),
            Err(error) => { eprintln!("compiler provider: {error}"); std::process::exit(2); }
        }
        return;
    }
    let t_start = std::time::Instant::now();
    // `MNCS_PROBE_MODULES` optionally narrows the elaborated module set
    // (comma-separated leaf names). By default it loads the shared frontend
    // and declaration core; focused suites load only their affected closure.
    // Each excluded module still needs current-pin verification before parity
    // can be claimed.
    let wanted: Option<Vec<String>> = std::env::var("MNCS_PROBE_MODULES").ok().map(|raw| {
        raw.split(',')
            .map(|part| part.trim().to_owned())
            .filter(|part| !part.is_empty())
            .collect()
    });
    // Keep the resolver closure loaded, but elaborate only the Program a
    // focused execution suite actually calls.
    let execution_modules: Option<Vec<String>> = std::env::var("MNCS_PROBE_EXECUTION_MODULES")
        .ok()
        .map(|raw| {
            raw.split(',')
                .map(|part| part.trim().to_owned())
                .filter(|part| !part.is_empty())
                .collect()
        });
    let generic_seeds: Vec<HostGenericSeedRequest> = std::env::var("MNCS_PROBE_GENERIC_SEEDS")
        .ok()
        .map(|raw| serde_json::from_str(&raw).expect("valid MNCS_PROBE_GENERIC_SEEDS JSON"))
        .unwrap_or_default();
    // The flow slice is optional so historical frontend/declaration probes
    // do not pay to elaborate its current CFG consumer unless requested.
    // This loader extension and the SSA oracle below stay only until the
    // canonical compiler test runner can make the same native-vs-Rust check
    // without this adapter; then both paths can be retired.
    let include_flow = wanted.as_ref().is_some_and(|names| {
        names
            .iter()
            .any(|name| matches!(name.as_str(), "flow" | "ssa" | "project"))
    });
    let mut sources = Sources(BTreeMap::new());
    for file in [
        "source", "lexer", "parser", "kernel", "segment", "decl", "flow", "ssa", "project",
    ] {
        if matches!(file, "flow" | "ssa" | "project") && !include_flow {
            continue;
        }
        if let Some(names) = &wanted {
            if !names.iter().any(|name| name == file) {
                continue;
            }
        }
        let text = std::fs::read_to_string(format!("src/compiler/{file}.mncs")).unwrap();
        sources
            .0
            .insert(format!("mncs.compiler.{file}.v1"), envelope(text));
    }
    let backend_name = std::env::var("MNCS_PROBE_BACKEND").ok();
    // Content-addressed readiness cache: frontend Programs and backend
    // artifacts keyed by every input that determines them (toolchain
    // identity, backend configuration/target, module closure bytes,
    // seed request, module selection). A hit skips elaboration and
    // lowering entirely; invalidation is exact because the key is the
    // content. Unset MNCS_PROBE_CACHE_DIR disables the cache
    // (historical behavior).
    let cache_dir = std::env::var("MNCS_PROBE_CACHE_DIR")
        .ok()
        .filter(|dir| !dir.is_empty());
    let seeds_raw = std::env::var("MNCS_PROBE_GENERIC_SEEDS").unwrap_or_default();
    // Gathered once: Stage-0 pin/provision/tree reads plus one rustc
    // version probe. Skipped entirely when the cache is disabled.
    let toolchain = cache_dir.as_ref().map(|_| {
        gather_toolchain_identity(
            backend_name.as_deref(),
            std::path::Path::new("mncs-language.lock.json"),
            std::path::Path::new(".bootstrap"),
        )
    });
    // Loaded closure as (name, bytes) pairs, read once (the envelopes
    // already hold the leaves but do not lend the text back).
    let closure: Vec<(String, String)> = [
        "source", "lexer", "parser", "kernel", "segment", "decl", "flow", "ssa", "project",
    ]
    .into_iter()
    .filter_map(|file| {
        let name = format!("mncs.compiler.{file}.v1");
        sources.0.contains_key(&name).then(|| {
            let text = std::fs::read_to_string(format!("src/compiler/{file}.mncs")).unwrap();
            (name, text)
        })
    })
    .collect();
    let mut cache_hits = 0u32;
    let mut cache_misses = 0u32;
    let cache_key = |module: &str| -> String {
        cache_key_hex(
            toolchain
                .as_ref()
                .expect("toolchain identity is gathered whenever the cache is enabled"),
            module,
            &closure,
            &seeds_raw,
        )
    };
    // Entries are gzip-compressed JSON
    // (`{identity, program, artifact|null}`) via the system gzip:
    // payloads shrink ~25x (hundreds of MB to tens) with no new
    // dependencies. Any compression failure degrades to a miss, never
    // an error.
    let cache_load =
        |key: &str| -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
            let dir = cache_dir.as_ref()?;
            let toolchain = toolchain.as_ref()?;
            let output = std::process::Command::new("gzip")
                .args(["-dc", &format!("{dir}/{key}.json.gz")])
                .output()
                .ok()?;
            if !output.status.success() {
                return None;
            }
            parse_cache_entry(&output.stdout, toolchain, backend_name.as_deref())
        };
    let cache_store = |key: &str,
                       program: &mncs_model::Program,
                       artifact: Option<&mncs_model::BackendArtifact>| {
        let (Some(dir), Some(toolchain)) = (cache_dir.as_ref(), toolchain.as_ref()) else {
            return;
        };
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
        let entry =
            serde_json::json!({"identity": toolchain, "program": program, "artifact": artifact});
        let entry_bytes = serde_json::to_vec(&entry).expect("entry serializes");
        // Compress via a temp file, not a stdin pipe: entries are hundreds
        // of MB, and piping both stdin and stdout through 64KB kernel
        // buffers deadlocks once both fill with no drainer.
        let raw_tmp = format!("{dir}/{key}.json.tmp");
        if std::fs::write(&raw_tmp, &entry_bytes).is_err() {
            return;
        }
        let output = std::process::Command::new("gzip")
            .args(["-n", "-c", &raw_tmp])
            .output();
        let _ = std::fs::remove_file(&raw_tmp);
        let Ok(output) = output else {
            return;
        };
        if !output.status.success() {
            return;
        }
        let tmp = format!("{dir}/{key}.json.gz.tmp");
        if std::fs::write(&tmp, &output.stdout).is_ok() {
            let _ = std::fs::rename(&tmp, format!("{dir}/{key}.json.gz"));
        }
    };
    let mut programs = BTreeMap::new();
    let mut cached_artifacts: BTreeMap<String, mncs_model::BackendArtifact> = BTreeMap::new();
    for (name, source) in &sources.0 {
        if execution_modules
            .as_ref()
            .is_some_and(|modules| !modules.iter().any(|module| module == name))
        {
            continue;
        }
        if cache_dir.is_some() {
            let key = cache_key(name);
            if let Some((program, artifact)) = cache_load(&key) {
                if backend_name.is_none() || artifact.is_some() {
                    cache_hits += 1;
                    if let Some(artifact) = artifact {
                        cached_artifacts.insert(name.clone(), artifact);
                    }
                    programs.insert(name.clone(), program);
                    continue;
                }
            }
            cache_misses += 1;
        }
        let module_seeds: Vec<_> = generic_seeds
            .iter()
            .filter(|seed| seed.module == *name)
            .cloned()
            .collect();
        let result = ReferenceCompiler::default().front_end_with_resolver_and_seeds(
            source.clone(),
            &sources,
            &module_seeds,
        );
        assert!(result.is_valid(), "{name}: {:?}", result.diagnostics);
        let program = result.program.unwrap();
        if backend_name.is_none() && cache_dir.is_some() {
            cache_store(&cache_key(name), &program, None);
        }
        programs.insert(name.clone(), program);
    }
    let sessions: BTreeMap<_, _> = programs
        .iter()
        .map(|(name, program)| (name.clone(), BodyExecutionSession::new(program)))
        .collect();
    // Optional development path: compile the exact loaded reference Program
    // once, retain its backend session, then answer the same requests through
    // that executable artifact. The ordinary interpreter remains available
    // as the independent comparison path.
    let backend_artifacts: BTreeMap<_, _> = backend_name
        .as_deref()
        .map(|backend| {
            let compiler = ReferenceCompiler::default();
            programs
                .iter()
                .map(|(name, program)| {
                    if let Some(artifact) = cached_artifacts.get(name) {
                        return (name.clone(), artifact.clone());
                    }
                    let emit = [
                        ArtifactRepresentation::Semantic,
                        ArtifactRepresentation::Hir,
                        ArtifactRepresentation::Ssa,
                        ArtifactRepresentation::TargetLoweringPlan,
                        ArtifactRepresentation::BackendArtifact,
                    ]
                    .into_iter()
                    .collect();
                    let request = compiler
                        .request_for_program_with_backend(program, emit, backend)
                        .unwrap_or_else(|error| panic!("{backend} request for {name}: {error:?}"));
                    let result = compiler.compile(request, program);
                    let artifact = result
                        .emissions
                        .backend
                        .as_ref()
                        .unwrap_or_else(|| {
                            panic!("{backend} emitted no artifact for {name}: {result:?}")
                        })
                        .clone();
                    if cache_dir.is_some() {
                        cache_store(&cache_key(name), program, Some(&artifact));
                    }
                    (name.clone(), artifact)
                })
                .collect()
        })
        .unwrap_or_default();
    let backend_sessions: BTreeMap<_, _> = backend_artifacts
        .iter()
        .map(|(name, artifact)| {
            (
                name.clone(),
                OwnedExecutionSession::new(artifact.clone())
                    .unwrap_or_else(|error| panic!("backend session for {name}: {error}")),
            )
        })
        .collect();
    // One-shot public emission (P-VM-COMPILER-003 substance for the
    // direct path): same module/seed/cache environment as the
    // transport, but prints one sealed artifact and exits so
    // out-of-process runtimes, stores, and evidence pipelines can
    // consume frozen bytes without linking the compiler or speaking
    // the JSONL protocol.
    if let Some(module) =
        std::env::args().find_map(|arg| arg.strip_prefix("--emit-vm-artifact=").map(str::to_owned))
    {
        let program = programs
            .get(&module)
            .unwrap_or_else(|| panic!("--emit-vm-artifact for unknown module {module}"));
        let compiler = ReferenceCompiler::default();
        let artifact = vm_emit::emit_vm_artifact(&compiler, program).expect("direct VM emission");
        println!("{}", json!({"module": module, "artifact": artifact}));
        return;
    }
    let stage0 = toolchain.as_ref().map(|identity| {
        format!(
            "{}/{}",
            short_hash(&identity.provisioned_revision),
            short_hash(&identity.lock_revision)
        )
    });
    if let Some(backend) = backend_name.as_deref() {
        let reused = backend_sessions
            .values()
            .filter(|session| session.reused())
            .count();
        let artifacts = backend_artifacts
            .iter()
            .map(|(name, artifact)| {
                (
                    name,
                    artifact.backend.name.as_str(),
                    artifact.artifact_kind.as_str(),
                    artifact.identity_is_valid(),
                )
            })
            .collect::<Vec<_>>();
        eprintln!("mncs-stage0-probe backend={backend} modules={} retained_sessions={reused} artifacts={artifacts:?} cache_hits={cache_hits} cache_misses={cache_misses} stage0={} ready_s={:.1}", backend_sessions.len(), stage0.as_deref().unwrap_or("cache=off"), t_start.elapsed().as_secs_f64());
    } else if cache_dir.is_some() {
        eprintln!("mncs-stage0-probe backend=none modules={} cache_hits={cache_hits} cache_misses={cache_misses} stage0={}", programs.len(), stage0.as_deref().unwrap_or("cache=off"));
    }
    for line in io::stdin().lock().lines() {
        // Local test transport over a pipe: requests carry whole native
        // Functions (deeply nested wire values), so the untrusted-input
        // recursion cap does not apply. Depth stays proportional to the
        // bounded fixture sources on this loop; never expose it to a
        // socket, and reach for serde_stacker before sending large
        // real-module Functions through verify-style requests.
        let line = line.unwrap();
        let mut deserializer = serde_json::Deserializer::from_str(&line);
        deserializer.disable_recursion_limit();
        let input: Value = serde::de::Deserialize::deserialize(&mut deserializer).unwrap();
        let output = if let Some(text) = input.get("oracle").and_then(Value::as_str) {
            serde_json::to_value(mncs_syntax::parse(&envelope(text.to_owned()))).unwrap()
        } else if input.get("execution_status").is_some() {
            json!({
                "backend": backend_name,
                "modules": backend_sessions.len(),
                "retained_sessions": backend_sessions.values().filter(|session| session.reused()).count(),
            })
        } else if let Some(module) = input.get("record_types").and_then(Value::as_str) {
            let program = &programs[module];
            json!(program.record_types)
        } else if let Some(request) = input.get("project_oracle") {
            let root = request["root"].as_str().expect("project root source");
            let mut imported = BTreeMap::new();
            for (module, text) in request["modules"].as_object().expect("project modules") {
                imported.insert(
                    module.clone(),
                    envelope(text.as_str().expect("module source").to_owned()),
                );
            }
            let resolver = Sources(imported);
            let result = ReferenceCompiler::default()
                .front_end_with_resolver(envelope(root.to_owned()), &resolver);
            let ssa = result
                .program
                .as_ref()
                .and_then(|program| program.lower_to_ssa().ok());
            let executions = request
                .get("calls")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(|call| {
                    let call: ExecutionRequest = serde_json::from_value(call.clone()).unwrap();
                    match (result.program.as_ref(), ssa.as_ref()) {
                        (Some(program), Some(ssa)) => {
                            json!(mncs_model::execute_ssa_module(program, ssa, &call))
                        }
                        _ => json!({"status": "invalid", "reason": "project oracle did not produce executable SSA"}),
                    }
                })
                .collect::<Vec<_>>();
            let program = result.program.as_ref().map(|program| {
                json!({
                    "module": program.module,
                    "dependencies": program.dependencies,
                    "functions": program.functions.iter().map(|function| json!({
                        "name": function.name,
                        "home_module": function.home_module,
                        "identity": mncs_model::function_id(
                            function.identity_namespace(&program.module),
                            &function.name,
                        ),
                        "inputs": function.inputs,
                        "outputs": function.outputs,
                    })).collect::<Vec<_>>(),
                    "record_types": program.record_types,
                    "finite_types": program.finite_types,
                })
            });
            json!({
                "valid": result.is_valid(),
                "diagnostics": result.diagnostics,
                "module_resolutions": result.module_resolutions,
                "program": program,
                "ssa": ssa,
                "executions": executions,
            })
        } else if let Some(request) = input.get("emit_vm_artifact") {
            // Direct canonical emission: one sealed mncs.vm.artifact/1
            // for an elaborated module, no research payload involved.
            let module = request
                .get("module")
                .and_then(Value::as_str)
                .expect("emit_vm_artifact module");
            let program = programs
                .get(module)
                .unwrap_or_else(|| panic!("emit_vm_artifact for unknown module {module}"));
            let compiler = ReferenceCompiler::default();
            let artifact =
                vm_emit::emit_vm_artifact(&compiler, program).expect("direct VM emission");
            json!({"module": module, "artifact": artifact})
        } else if let Some(text) = input.get("elaborate").and_then(Value::as_str) {
            let result = ReferenceCompiler::default()
                .front_end_with_resolver(envelope(text.to_owned()), &NullResolver);
            serde_json::to_value(&result.diagnostics).unwrap()
        } else if let Some(text) = input.get("ssa").and_then(Value::as_str) {
            // Test-only Rust oracle: flow parity currently compares block
            // shape with Stage-0 SSA because MNCS has not built value SSA.
            // Remove this transport path when the canonical compiler test
            // runner can execute the same Rust SSA comparison directly.
            let result = ReferenceCompiler::default()
                .front_end_with_resolver(envelope(text.to_owned()), &NullResolver);
            let ssa = result
                .program
                .as_ref()
                .and_then(|program| program.lower_to_ssa().ok());
            json!({"diagnostics": result.diagnostics, "ssa": ssa})
        } else {
            let request: ExecutionRequest = serde_json::from_value(input).unwrap();
            if let Some(session) = backend_sessions.get(&request.target.module) {
                json!(session.execute(&request))
            } else {
                let session = &sessions[&request.target.module];
                json!(session.execute(&request))
            }
        };
        println!("{}", output);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mncs_model::{BackendArtifact, BackendIdentity, CompilerArtifactRef, TransformationStatus};

    /// Hand-built identity with real in-memory driver parts and fixed
    /// file/toolchain parts, so each mutation test changes exactly one
    /// component.
    fn test_toolchain(backend_name: Option<&str>) -> ToolchainIdentity {
        let compiler = ReferenceCompiler::default();
        let (compiler_host, build_host) = native_host_identities();
        ToolchainIdentity {
            schema: TOOLCHAIN_IDENTITY_SCHEMA.to_owned(),
            lock_revision: "lock-rev-a".to_owned(),
            lock_source_profile: "0.18".to_owned(),
            provisioned_revision: "prov-rev-a".to_owned(),
            bootstrap_tree_sha256: "tree-a".to_owned(),
            bootstrap_cargo_lock_sha256: "cargolock-a".to_owned(),
            compiler_implementation: compiler.identity.clone(),
            pipeline_identity: compiler.pipeline.identity.clone(),
            language_profile: REFERENCE_LANGUAGE_PROFILE.to_owned(),
            backend_name: backend_name.map(str::to_owned),
            backend_configuration: backend_name.and_then(configuration_for_backend),
            target: backend_name.and_then(target_for_backend),
            compiler_host,
            build_host,
            rustc_version: "rustc-test".to_owned(),
        }
    }

    fn test_closure() -> Vec<(String, String)> {
        vec![("mncs.compiler.decl.v1".to_owned(), "decl-bytes".to_owned())]
    }

    fn tiny_program() -> mncs_model::Program {
        let text = "mncs 0.10;\nmodule probe.tiny;\nrecord Token { kind: u64, start: u64, end: u64 }\nfn first(tokens: [Token; 2]) -> (result: Token) { return tokens[0]; }\n";
        let result = ReferenceCompiler::default()
            .front_end_with_resolver(envelope(text.to_owned()), &NullResolver);
        assert!(
            result.is_valid(),
            "tiny program valid: {:?}",
            result.diagnostics
        );
        result.program.unwrap()
    }

    fn test_artifact() -> BackendArtifact {
        BackendArtifact::new(
            BackendIdentity::new("test-backend", "0.1"),
            CompilerArtifactRef::new(ArtifactRepresentation::Semantic, "test-schema", "test-fp"),
            TargetContractRef::new("test-target", BTreeMap::new(), Vec::new(), Vec::new()),
            "test-format",
            b"bytes",
            vec!["f".to_owned()],
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            TransformationStatus::Pass,
        )
    }

    #[test]
    fn same_full_identity_hits() {
        let toolchain = test_toolchain(Some("cranelift"));
        let closure = test_closure();
        assert_eq!(
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &closure, "seeds"),
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &closure, "seeds"),
        );
    }

    #[test]
    fn source_change_misses() {
        let toolchain = test_toolchain(Some("cranelift"));
        let before = test_closure();
        let after = vec![(
            "mncs.compiler.decl.v1".to_owned(),
            "decl-bytes-changed".to_owned(),
        )];
        assert_ne!(
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &before, "seeds"),
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &after, "seeds"),
        );
    }

    #[test]
    fn seed_change_misses() {
        let toolchain = test_toolchain(Some("cranelift"));
        let closure = test_closure();
        assert_ne!(
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &closure, "seeds-a"),
            cache_key_hex(&toolchain, "mncs.compiler.decl.v1", &closure, "seeds-b"),
        );
    }

    #[test]
    fn backend_change_misses() {
        let cranelift = test_toolchain(Some("cranelift"));
        let bytecode = test_toolchain(Some("research-bytecode"));
        assert!(cranelift.backend_configuration.is_some());
        assert!(bytecode.backend_configuration.is_some());
        let closure = test_closure();
        assert_ne!(
            cache_key_hex(&cranelift, "m", &closure, ""),
            cache_key_hex(&bytecode, "m", &closure, ""),
        );
        let none = test_toolchain(None);
        assert_ne!(
            cache_key_hex(&cranelift, "m", &closure, ""),
            cache_key_hex(&none, "m", &closure, ""),
        );
    }

    #[test]
    fn stage0_and_toolchain_change_misses() {
        let base = test_toolchain(Some("cranelift"));
        let closure = test_closure();
        let base_key = cache_key_hex(&base, "m", &closure, "");
        let mutations: [fn(&mut ToolchainIdentity); 9] = [
            |t: &mut ToolchainIdentity| t.lock_revision = "lock-rev-b".to_owned(),
            |t: &mut ToolchainIdentity| t.lock_source_profile = "0.19".to_owned(),
            |t: &mut ToolchainIdentity| t.provisioned_revision = "prov-rev-b".to_owned(),
            |t: &mut ToolchainIdentity| t.bootstrap_tree_sha256 = "tree-b".to_owned(),
            |t: &mut ToolchainIdentity| t.bootstrap_cargo_lock_sha256 = "cargolock-b".to_owned(),
            |t: &mut ToolchainIdentity| t.compiler_implementation.version = "9.9.9".to_owned(),
            |t: &mut ToolchainIdentity| t.compiler_host.architecture = "other-arch".to_owned(),
            |t: &mut ToolchainIdentity| t.build_host.toolchain = "rust-9.9.9".to_owned(),
            |t: &mut ToolchainIdentity| t.rustc_version = "rustc-other".to_owned(),
        ];
        for mutate in mutations {
            let mut changed = base.clone();
            mutate(&mut changed);
            assert_ne!(
                base_key,
                cache_key_hex(&changed, "m", &closure, ""),
                "toolchain mutation must miss"
            );
        }
    }

    #[test]
    fn corrupt_or_stale_entry_is_safe_miss() {
        let toolchain = test_toolchain(None);
        // Not JSON at all.
        assert!(parse_cache_entry(b"not json", &toolchain, None).is_none());
        // Valid JSON but no entry shape.
        assert!(parse_cache_entry(b"{}", &toolchain, None).is_none());
        let program = tiny_program();
        // Well-formed entry under a different toolchain identity.
        let mut other = toolchain.clone();
        other.provisioned_revision = "prov-rev-b".to_owned();
        let stale = serde_json::to_vec(&serde_json::json!({
            "identity": other,
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        assert!(parse_cache_entry(&stale, &toolchain, None).is_none());
        // Legacy v1 entry without an identity echo.
        let legacy = serde_json::to_vec(&serde_json::json!({
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        assert!(parse_cache_entry(&legacy, &toolchain, None).is_none());
        // Matching identity but an artifact that fails validation.
        let backended = test_toolchain(Some("cranelift"));
        let mut tampered = test_artifact();
        tampered.identity = SemanticId("mncs:bogus:artifact".to_owned());
        assert!(!tampered.identity_is_valid());
        let bad_artifact = serde_json::to_vec(&serde_json::json!({
            "identity": backended,
            "program": program,
            "artifact": tampered,
        }))
        .unwrap();
        assert!(parse_cache_entry(&bad_artifact, &backended, Some("cranelift")).is_none());
        // Matching identity but an unparseable artifact.
        let unparseable = serde_json::to_vec(&serde_json::json!({
            "identity": backended,
            "program": program,
            "artifact": {"broken": true},
        }))
        .unwrap();
        assert!(parse_cache_entry(&unparseable, &backended, Some("cranelift")).is_none());
    }

    #[test]
    fn round_trip_entry_hits() {
        let program = tiny_program();
        let plain = test_toolchain(None);
        let entry = serde_json::to_vec(&serde_json::json!({
            "identity": plain,
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        let (loaded, artifact) = parse_cache_entry(&entry, &plain, None).expect("hit");
        assert!(artifact.is_none());
        assert_eq!(loaded.module, program.module);
        let backended = test_toolchain(Some("cranelift"));
        let artifact = test_artifact();
        assert!(artifact.identity_is_valid());
        let entry = serde_json::to_vec(&serde_json::json!({
            "identity": backended,
            "program": program,
            "artifact": artifact,
        }))
        .unwrap();
        assert!(parse_cache_entry(&entry, &backended, Some("cranelift"))
            .is_some_and(|(_, artifact)| artifact.is_some()));
    }

    #[test]
    fn gather_reads_pins_and_tree() {
        let root =
            std::env::temp_dir().join(format!("probe-cache-test-{}-gather", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let bootstrap = root.join("bootstrap");
        std::fs::create_dir_all(bootstrap.join("crates")).unwrap();
        std::fs::write(
            root.join("lock.json"),
            r#"{"revision": "rev-1", "source_profile": "0.18"}"#,
        )
        .unwrap();
        std::fs::write(bootstrap.join("revision"), "rev-1\n").unwrap();
        std::fs::write(bootstrap.join("Cargo.lock"), "lock-bytes").unwrap();
        std::fs::write(bootstrap.join("crates").join("a.rs"), "fn a() {}").unwrap();
        // Build outputs must not affect the tree digest.
        std::fs::create_dir_all(bootstrap.join("target").join("release")).unwrap();
        std::fs::write(
            bootstrap.join("target").join("release").join("big"),
            vec![7u8; 1024],
        )
        .unwrap();
        let first = gather_toolchain_identity(None, &root.join("lock.json"), &bootstrap);
        assert_eq!(first.lock_revision, "rev-1");
        assert_eq!(first.lock_source_profile, "0.18");
        assert_eq!(first.provisioned_revision, "rev-1");
        assert_ne!(first.bootstrap_tree_sha256, UNKNOWN_IDENTITY_PART);
        // A target/ rebuild does not change the tree digest.
        std::fs::write(
            bootstrap.join("target").join("release").join("big"),
            vec![8u8; 2048],
        )
        .unwrap();
        let second = gather_toolchain_identity(None, &root.join("lock.json"), &bootstrap);
        assert_eq!(first.bootstrap_tree_sha256, second.bootstrap_tree_sha256);
        // A source change does.
        std::fs::write(bootstrap.join("crates").join("a.rs"), "fn a() { 1 }").unwrap();
        let third = gather_toolchain_identity(None, &root.join("lock.json"), &bootstrap);
        assert_ne!(first.bootstrap_tree_sha256, third.bootstrap_tree_sha256);
        // Missing files degrade to explicit unknowns, never a panic.
        let missing =
            gather_toolchain_identity(None, &root.join("no-lock.json"), &root.join("no-bootstrap"));
        assert_eq!(missing.lock_revision, UNKNOWN_IDENTITY_PART);
        assert_eq!(missing.provisioned_revision, UNKNOWN_IDENTITY_PART);
        assert_eq!(missing.bootstrap_tree_sha256, UNKNOWN_IDENTITY_PART);
        assert_eq!(missing.bootstrap_cargo_lock_sha256, UNKNOWN_IDENTITY_PART);
        let _ = std::fs::remove_dir_all(&root);
    }
}
