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
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, BufWriter, Read, Write},
    sync::{Mutex, OnceLock},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

mod provider;
#[path = "../../vm_emit.rs"]
mod vm_emit;

static TELEMETRY_ORIGIN: OnceLock<Instant> = OnceLock::new();
static TELEMETRY_WRITER: OnceLock<Option<Mutex<BufWriter<File>>>> = OnceLock::new();

fn telemetry_enabled() -> bool {
    matches!(
        std::env::var("MNCS_PROBE_TELEMETRY").as_deref(),
        Ok("1" | "true")
    )
}

fn trace_event(mut event: Value) {
    if !telemetry_enabled() {
        return;
    }
    let origin = TELEMETRY_ORIGIN.get_or_init(Instant::now);
    let Some(fields) = event.as_object_mut() else {
        return;
    };
    fields.insert("pid".to_owned(), json!(std::process::id()));
    fields.insert(
        "process_elapsed_ms".to_owned(),
        json!(origin.elapsed().as_secs_f64() * 1000.0),
    );
    let Ok(line) = serde_json::to_vec(&event) else {
        return;
    };
    let trace_path = std::env::var_os("MNCS_PROBE_TRACE_PATH");
    if let Some(path) = trace_path {
        let writer = TELEMETRY_WRITER.get_or_init(|| {
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)
                .ok()
                .map(|file| Mutex::new(BufWriter::new(file)))
        });
        if let Some(writer) = writer {
            if let Ok(mut writer) = writer.lock() {
                let _ = writer.write_all(&line);
                let _ = writer.write_all(b"\n");
                let _ = writer.flush();
                return;
            }
        }
    }
    eprintln!("{}", String::from_utf8_lossy(&line));
}

fn trace_phase(phase: &str, started: Instant, details: Value) {
    if !telemetry_enabled() {
        return;
    }
    let mut fields = details.as_object().cloned().unwrap_or_default();
    fields.insert("event".to_owned(), json!("phase"));
    fields.insert("phase".to_owned(), json!(phase));
    fields.insert(
        "duration_ms".to_owned(),
        json!(started.elapsed().as_secs_f64() * 1000.0),
    );
    trace_event(Value::Object(fields));
}

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

#[derive(serde::Serialize)]
struct ProbeCacheEntry<'a> {
    identity: &'a ToolchainIdentity,
    program: &'a mncs_model::Program,
    artifact: Option<&'a mncs_model::BackendArtifact>,
}

#[derive(serde::Deserialize)]
struct ProbeCacheProgramEntry {
    identity: ToolchainIdentity,
    program: mncs_model::Program,
}

#[derive(serde::Deserialize)]
struct ProbeCacheBackendEntry {
    identity: ToolchainIdentity,
    program: mncs_model::Program,
    artifact: Option<mncs_model::BackendArtifact>,
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

/// Frontend Programs are independent of backend selection. Keep their cache
/// identity separate from the backend artifact key so a backend change can
/// reuse the exact elaborated Program without reusing an incompatible
/// executable artifact.
fn frontend_toolchain_identity(toolchain: &ToolchainIdentity) -> ToolchainIdentity {
    let mut identity = toolchain.clone();
    identity.backend_name = None;
    identity.backend_configuration = None;
    identity.target = None;
    identity
}

fn module_seeds_for(module: &str, seeds: &[HostGenericSeedRequest]) -> Vec<HostGenericSeedRequest> {
    seeds
        .iter()
        .filter(|seed| seed.module == module)
        .cloned()
        .collect()
}

fn frontend_cache_key_hex(
    toolchain: &ToolchainIdentity,
    module: &str,
    closure: &[(String, String)],
    module_seeds: &[HostGenericSeedRequest],
) -> String {
    let frontend_identity = frontend_toolchain_identity(toolchain);
    let seed_identity = serde_json::to_string(module_seeds)
        .expect("module-specific specialization seeds serialize");
    let content_key = cache_key_hex(&frontend_identity, module, closure, &seed_identity);
    let domain = format!("probe-frontend-program-v1\n{content_key}");
    mncs_model::sha256_hex(domain.as_bytes())
}

/// Parse one decompressed cache entry. Every failure mode is a safe
/// miss (`None`): corrupt bytes, missing identity or program, a stored
/// toolchain identity that differs from the current one, or an artifact
/// whose own identity does not validate.
#[cfg(test)]
fn parse_cache_entry(
    raw: &[u8],
    expected: &ToolchainIdentity,
    backend_name: Option<&str>,
) -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
    parse_cache_entry_reader(raw, expected, backend_name)
}

fn parse_cache_entry_reader<R: Read>(
    reader: R,
    expected: &ToolchainIdentity,
    backend_name: Option<&str>,
) -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
    if backend_name.is_none() {
        let entry: ProbeCacheProgramEntry = serde_json::from_reader(reader).ok()?;
        if &entry.identity != expected {
            return None;
        }
        return Some((entry.program, None));
    }
    let entry: ProbeCacheBackendEntry = serde_json::from_reader(reader).ok()?;
    if &entry.identity != expected {
        return None;
    }
    let Some(artifact) = entry.artifact else {
        // A backend-specific entry may contain a valid frontend Program
        // whose backend compilation was interrupted. Reuse the Program and
        // compile the backend again; only a non-null artifact is reusable as
        // an executable session.
        return Some((entry.program, None));
    };
    if !artifact.identity_is_valid() {
        return None;
    }
    Some((entry.program, Some(artifact)))
}

fn short_hash(value: &str) -> &str {
    &value[..value.len().min(12)]
}
fn main() {
    if std::env::args().any(|arg| arg == "--producer-info") {
        println!("{}", provider::producer());
        return;
    }
    if let Some(path) =
        std::env::args().find_map(|arg| arg.strip_prefix("--provider-request=").map(str::to_owned))
    {
        match provider::run(&path) {
            Ok(value) => println!("{}", value),
            Err(error) => {
                eprintln!("compiler provider: {error}");
                std::process::exit(2);
            }
        }
        return;
    }
    let t_start = std::time::Instant::now();
    trace_event(json!({
        "event": "probe_start",
        "backend": std::env::var("MNCS_PROBE_BACKEND").ok(),
        "pid": std::process::id()
    }));
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
    let source_resolution_started = Instant::now();
    let mut sources = Sources(BTreeMap::new());
    let mut loaded_source_bytes = 0usize;
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
        loaded_source_bytes += text.len();
        sources
            .0
            .insert(format!("mncs.compiler.{file}.v1"), envelope(text));
    }
    trace_phase(
        "source_resolution",
        source_resolution_started,
        json!({"module_count": sources.0.len(), "source_bytes": loaded_source_bytes}),
    );
    let backend_name = std::env::var("MNCS_PROBE_BACKEND").ok();
    // Content-addressed readiness cache: backend artifacts retain their
    // existing backend-specific key, while frontend Programs also have a
    // backend-independent key over the Stage-0/compiler identity, resolver
    // source closure, module, and that module's specialization seeds. A
    // Program hit skips elaboration; it never admits an artifact from a
    // different backend. Unset MNCS_PROBE_CACHE_DIR disables the cache.
    let cache_dir = std::env::var("MNCS_PROBE_CACHE_DIR")
        .ok()
        .filter(|dir| !dir.is_empty());
    let seeds_raw = std::env::var("MNCS_PROBE_GENERIC_SEEDS").unwrap_or_default();
    // Gathered once: Stage-0 pin/provision/tree reads plus one rustc
    // version probe. Skipped entirely when the cache is disabled.
    let toolchain = cache_dir.as_ref().map(|_| {
        let started = Instant::now();
        let identity = gather_toolchain_identity(
            backend_name.as_deref(),
            std::path::Path::new("mncs-language.lock.json"),
            std::path::Path::new(".bootstrap"),
        );
        trace_phase(
            "toolchain_identity",
            started,
            json!({
                "backend": backend_name,
                "stage0_revision": identity.provisioned_revision,
                "lock_revision": identity.lock_revision,
                "cache_enabled": true
            }),
        );
        identity
    });
    let frontend_identity = toolchain.as_ref().map(frontend_toolchain_identity);
    // Loaded closure as (name, bytes) pairs, read once (the envelopes
    // already hold the leaves but do not lend the text back).
    let closure_started = Instant::now();
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
    trace_phase(
        "dependency_closure_resolution",
        closure_started,
        json!({
            "module_count": closure.len(),
            "source_bytes": closure.iter().map(|(_, source)| source.len()).sum::<usize>()
        }),
    );
    let mut cache_hits = 0u32;
    let mut cache_misses = 0u32;
    let mut backend_artifact_cache_hits = 0u32;
    let mut frontend_program_cache_hits = 0u32;
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
    let frontend_cache_key = |module: &str, module_seeds: &[HostGenericSeedRequest]| -> String {
        frontend_cache_key_hex(
            frontend_identity
                .as_ref()
                .expect("frontend identity is gathered whenever the cache is enabled"),
            module,
            &closure,
            module_seeds,
        )
    };
    // Entries are gzip-compressed JSON
    // (`{identity, program, artifact|null}`) via the system gzip:
    // payloads shrink ~25x (hundreds of MB to tens) with no new
    // dependencies. Any compression failure degrades to a miss, never
    // an error.
    let cache_load = |key: &str,
                      suffix: &str,
                      identity: &ToolchainIdentity,
                      expected_backend: Option<&str>|
     -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
        let dir = cache_dir.as_ref()?;
        let path = format!("{dir}/{key}{suffix}");
        let started = Instant::now();
        let mut gzip = std::process::Command::new("gzip")
            .args(["-dc", &path])
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
            .ok()?;
        let gzip_pid = gzip.id();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "artifact_cache_gzip_read",
            "cache_key": key,
            "cache_file_suffix": suffix,
            "gzip_pid": gzip_pid
        }));
        let reader = gzip.stdout.take()?;
        let reader = BufReader::with_capacity(1024 * 1024, reader);
        let parsed = parse_cache_entry_reader(reader, identity, expected_backend);
        let status = gzip.wait().ok()?;
        trace_phase(
            "artifact_cache_gzip_read",
            started,
            json!({
                "cache_key": key,
                "cache_file_suffix": suffix,
                "gzip_pid": gzip_pid,
                "exit_code": status.code(),
                "success": status.success(),
                "compressed_bytes": std::fs::metadata(path).map(|metadata| metadata.len()).ok()
            }),
        );
        if status.success() {
            parsed
        } else {
            None
        }
    };
    let cache_store = |key: &str,
                       suffix: &str,
                       identity: &ToolchainIdentity,
                       program: &mncs_model::Program,
                       artifact: Option<&mncs_model::BackendArtifact>| {
        let Some(dir) = cache_dir.as_ref() else {
            return;
        };
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
        let unique_suffix = format!(
            "{}.{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        );
        let raw_tmp = format!("{dir}/{key}.{unique_suffix}.json.tmp");
        let encode_started = Instant::now();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "artifact_cache_encode",
            "cache_key": key,
            "cache_file_suffix": suffix,
            "artifact_present": artifact.is_some()
        }));
        let encoded_bytes = (|| -> io::Result<u64> {
            let file = File::create(&raw_tmp)?;
            let mut writer = BufWriter::new(file);
            serde_json::to_writer(
                &mut writer,
                &ProbeCacheEntry {
                    identity,
                    program,
                    artifact,
                },
            )
            .map_err(|error| io::Error::new(io::ErrorKind::Other, error))?;
            writer.flush()?;
            drop(writer);
            Ok(std::fs::metadata(&raw_tmp)?.len())
        })();
        let encoded_bytes = match encoded_bytes {
            Ok(bytes) => bytes,
            Err(error) => {
                let _ = std::fs::remove_file(&raw_tmp);
                trace_phase(
                    "artifact_cache_encode",
                    encode_started,
                    json!({"cache_key": key, "success": false, "error": error.to_string()}),
                );
                return;
            }
        };
        trace_phase(
            "artifact_cache_encode",
            encode_started,
            json!({"cache_key": key, "uncompressed_bytes": encoded_bytes}),
        );
        let compressed_tmp = format!("{dir}/{key}.{unique_suffix}.json.gz.tmp");
        let gzip_started = Instant::now();
        let compressed_file = match File::create(&compressed_tmp) {
            Ok(file) => file,
            Err(error) => {
                let _ = std::fs::remove_file(&raw_tmp);
                trace_phase(
                    "artifact_cache_gzip_write",
                    gzip_started,
                    json!({"cache_key": key, "success": false, "error": error.to_string()}),
                );
                return;
            }
        };
        let mut gzip = match std::process::Command::new("gzip")
            .args(["-n", "-c", &raw_tmp])
            .stdout(std::process::Stdio::from(compressed_file))
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(error) => {
                let _ = std::fs::remove_file(&raw_tmp);
                let _ = std::fs::remove_file(&compressed_tmp);
                trace_phase(
                    "artifact_cache_gzip_write",
                    gzip_started,
                    json!({"cache_key": key, "success": false, "error": error.to_string()}),
                );
                return;
            }
        };
        let gzip_pid = gzip.id();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "artifact_cache_gzip_write",
            "cache_key": key,
            "gzip_pid": gzip_pid
        }));
        let status = gzip.wait();
        let _ = std::fs::remove_file(&raw_tmp);
        let compressed_bytes = std::fs::metadata(&compressed_tmp)
            .map(|metadata| metadata.len())
            .ok();
        let status = match status {
            Ok(status) if status.success() => status,
            Ok(status) => {
                let _ = std::fs::remove_file(&compressed_tmp);
                trace_phase(
                    "artifact_cache_gzip_write",
                    gzip_started,
                    json!({
                        "cache_key": key,
                        "success": false,
                        "gzip_pid": gzip_pid,
                        "exit_code": status.code()
                    }),
                );
                return;
            }
            Err(error) => {
                let _ = std::fs::remove_file(&compressed_tmp);
                trace_phase(
                    "artifact_cache_gzip_write",
                    gzip_started,
                    json!({
                        "cache_key": key,
                        "success": false,
                        "gzip_pid": gzip_pid,
                        "error": error.to_string()
                    }),
                );
                return;
            }
        };
        let stored = compressed_bytes.is_some()
            && std::fs::rename(&compressed_tmp, format!("{dir}/{key}{suffix}")).is_ok();
        if !stored {
            let _ = std::fs::remove_file(&compressed_tmp);
        }
        trace_phase(
            "artifact_cache_gzip_write",
            gzip_started,
            json!({
                "cache_key": key,
                "cache_file_suffix": suffix,
                "success": stored,
                "gzip_pid": gzip_pid,
                "exit_code": status.code(),
                "uncompressed_bytes": encoded_bytes,
                "compressed_bytes": compressed_bytes
            }),
        );
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
        let module_seeds = module_seeds_for(name, &generic_seeds);
        if cache_dir.is_some() {
            let key = cache_key(name);
            let identity = toolchain
                .as_ref()
                .expect("toolchain identity is gathered whenever the cache is enabled");

            // Preserve the existing backend-specific cache key so prior
            // artifacts remain reusable without weakening their identity.
            if backend_name.is_some() {
                let cache_started = Instant::now();
                trace_event(json!({
                    "event": "phase_begin",
                    "phase": "artifact_cache_read_decode",
                    "module": name,
                    "cache_key": key,
                    "cache_file_suffix": ".json.gz"
                }));
                let cached = cache_load(&key, ".json.gz", identity, backend_name.as_deref());
                trace_phase(
                    "artifact_cache_read_decode",
                    cache_started,
                    json!({
                        "module": name,
                        "cache_key": key,
                        "decoded": cached.is_some(),
                        "usable": cached.is_some()
                    }),
                );
                if let Some((program, artifact)) = cached {
                    cache_hits += 1;
                    backend_artifact_cache_hits += u32::from(artifact.is_some());
                    let frontend_key = frontend_cache_key(name, &module_seeds);
                    let frontend_path = format!(
                        "{}/{frontend_key}.program.json.gz",
                        cache_dir.as_ref().unwrap()
                    );
                    if !std::path::Path::new(&frontend_path).is_file() {
                        cache_store(
                            &frontend_key,
                            ".program.json.gz",
                            frontend_identity.as_ref().unwrap(),
                            &program,
                            None,
                        );
                    }
                    if let Some(artifact) = artifact {
                        cached_artifacts.insert(name.clone(), artifact);
                    }
                    programs.insert(name.clone(), program);
                    continue;
                }
            }

            // A frontend Program depends on the Stage-0/compiler identity,
            // resolver source closure, this module, and only this module's
            // specialization seeds. Backend configuration affects the
            // executable artifact but cannot affect frontend elaboration.
            let frontend_key = frontend_cache_key(name, &module_seeds);
            let frontend_identity = frontend_identity.as_ref().unwrap();
            let frontend_started = Instant::now();
            trace_event(json!({
                "event": "phase_begin",
                "phase": "frontend_program_cache_read_decode",
                "module": name,
                "cache_key": frontend_key,
                "cache_file_suffix": ".program.json.gz"
            }));
            let mut cached_program =
                cache_load(&frontend_key, ".program.json.gz", frontend_identity, None);
            // Read old no-backend Program entries once as a migration path;
            // successful reads are written under the backend-independent key.
            if cached_program.is_none() {
                let legacy_key = cache_key_hex(frontend_identity, name, &closure, &seeds_raw);
                cached_program = cache_load(&legacy_key, ".json.gz", frontend_identity, None);
                if cached_program.is_some() {
                    if let Some((program, _)) = cached_program.as_ref() {
                        cache_store(
                            &frontend_key,
                            ".program.json.gz",
                            frontend_identity,
                            program,
                            None,
                        );
                    }
                }
            }
            trace_phase(
                "frontend_program_cache_read_decode",
                frontend_started,
                json!({
                    "module": name,
                    "cache_key": frontend_key,
                    "decoded": cached_program.is_some(),
                    "usable": cached_program.is_some()
                }),
            );
            if let Some((program, _)) = cached_program {
                cache_hits += 1;
                frontend_program_cache_hits += 1;
                programs.insert(name.clone(), program);
                continue;
            }
            cache_misses += 1;
        }
        trace_event(json!({
            "event": "phase_begin",
            "phase": "stage0_frontend_elaboration_specialization",
            "module": name,
            "specialization_seeds": module_seeds.iter().map(|seed| {
                json!({"function": seed.function, "type_arguments": seed.type_arguments})
            }).collect::<Vec<_>>()
        }));
        let frontend_started = Instant::now();
        let result = ReferenceCompiler::default().front_end_with_resolver_and_seeds(
            source.clone(),
            &sources,
            &module_seeds,
        );
        let frontend_valid = result.is_valid();
        trace_phase(
            "stage0_frontend_elaboration_specialization",
            frontend_started,
            json!({
                "module": name,
                "specialization_seed_count": module_seeds.len(),
                "valid": frontend_valid,
                "module_resolution_count": result.module_resolutions.len(),
                "program_function_count": result.program.as_ref().map(|program| program.functions.len()),
                "diagnostic_count": result.diagnostics.len()
            }),
        );
        assert!(frontend_valid, "{name}: {:?}", result.diagnostics);
        let program = result.program.unwrap();
        if cache_dir.is_some() {
            // Preserve the frontend result before backend compilation under
            // a backend-independent identity. An interrupted backend compile
            // or a later backend selection can reuse this exact Program.
            let frontend_key = frontend_cache_key(name, &module_seeds);
            cache_store(
                &frontend_key,
                ".program.json.gz",
                frontend_identity.as_ref().unwrap(),
                &program,
                None,
            );
        }
        programs.insert(name.clone(), program);
    }
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
                    if let Some(artifact) = cached_artifacts.remove(name) {
                        trace_event(json!({
                            "event": "artifact_reuse",
                            "phase": "backend_compile",
                            "module": name,
                            "source": "content_addressed_cache"
                        }));
                        return (name.clone(), artifact);
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
                    let request_started = Instant::now();
                    let request = compiler
                        .request_for_program_with_backend(program, emit, backend)
                        .unwrap_or_else(|error| panic!("{backend} request for {name}: {error:?}"));
                    trace_phase(
                        "backend_request_construction",
                        request_started,
                        json!({"module": name, "backend": backend}),
                    );
                    trace_event(json!({
                        "event": "phase_begin",
                        "phase": "backend_compilation",
                        "module": name,
                        "backend": backend
                    }));
                    let compile_started = Instant::now();
                    let mut result = compiler.compile(request, program);
                    let artifact = result.emissions.backend.take().unwrap_or_else(|| {
                        panic!("{backend} emitted no artifact for {name}: {result:?}")
                    });
                    // The retained artifact is the only compiler emission
                    // needed below; release the rest before cache encoding.
                    drop(result);
                    let payload_bytes = artifact.bytes_hex.len() / 2;
                    trace_phase(
                        "backend_compilation",
                        compile_started,
                        json!({
                            "module": name,
                            "backend": backend,
                            "artifact_kind": artifact.artifact_kind,
                            "artifact_payload_bytes": payload_bytes,
                            "identity_valid": artifact.identity_is_valid()
                        }),
                    );
                    if cache_dir.is_some() {
                        cache_store(
                            &cache_key(name),
                            ".json.gz",
                            toolchain.as_ref().unwrap(),
                            program,
                            Some(&artifact),
                        );
                    }
                    (name.clone(), artifact)
                })
                .collect()
        })
        .unwrap_or_default();
    let artifact_summaries = backend_artifacts
        .iter()
        .map(|(name, artifact)| {
            (
                name.clone(),
                artifact.backend.name.clone(),
                artifact.artifact_kind.clone(),
                artifact.identity_is_valid(),
            )
        })
        .collect::<Vec<_>>();
    let session_admission_started = Instant::now();
    let backend_sessions: BTreeMap<_, _> = backend_artifacts
        .into_iter()
        .map(|(name, artifact)| {
            let backend = artifact.backend.name.clone();
            let artifact_kind = artifact.artifact_kind.clone();
            let artifact_payload_bytes = artifact.bytes_hex.len() / 2;
            trace_event(json!({
                "event": "phase_begin",
                "phase": "retained_session_admission",
                "module": name,
                "backend": backend,
                "artifact_payload_bytes": artifact_payload_bytes
            }));
            let started = Instant::now();
            let session = OwnedExecutionSession::new(artifact)
                .unwrap_or_else(|error| panic!("backend session for {name}: {error}"));
            trace_phase(
                "retained_session_admission",
                started,
                json!({
                    "module": name,
                    "backend": backend,
                    "artifact_kind": artifact_kind,
                    "artifact_payload_bytes": artifact_payload_bytes,
                    "admitted": true
                }),
            );
            (name.clone(), session)
        })
        .collect();
    trace_phase(
        "retained_session_admission_total",
        session_admission_started,
        json!({
            "session_count": backend_sessions.len(),
            "retained_sessions": backend_sessions.values().filter(|session| session.reused()).count()
        }),
    );
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
        eprintln!("mncs-stage0-probe backend={backend} modules={} retained_sessions={reused} artifacts={artifact_summaries:?} cache_hits={cache_hits} backend_artifact_cache_hits={backend_artifact_cache_hits} frontend_program_cache_hits={frontend_program_cache_hits} cache_misses={cache_misses} stage0={} ready_s={:.1}", backend_sessions.len(), stage0.as_deref().unwrap_or("cache=off"), t_start.elapsed().as_secs_f64());
    } else if cache_dir.is_some() {
        eprintln!("mncs-stage0-probe backend=none modules={} cache_hits={cache_hits} frontend_program_cache_hits={frontend_program_cache_hits} cache_misses={cache_misses} stage0={}", programs.len(), stage0.as_deref().unwrap_or("cache=off"));
    }
    let release_programs_after_identities = backend_name.is_some()
        && std::env::var("MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES").as_deref() == Ok("1");
    if release_programs_after_identities {
        let mut record_types_cache = BTreeMap::<String, Value>::new();
        for line in io::stdin().lock().lines() {
            let line = line.unwrap();
            let parse_started = Instant::now();
            trace_event(json!({
                "event": "phase_begin",
                "phase": "request_json_decode",
                "request_bytes": line.len()
            }));
            let mut deserializer = serde_json::Deserializer::from_str(&line);
            deserializer.disable_recursion_limit();
            let input: Value = serde::de::Deserialize::deserialize(&mut deserializer).unwrap();
            let request_kind = if input.get("execution_status").is_some() {
                "execution_status"
            } else if input.get("record_types").is_some() {
                "record_types"
            } else {
                "mncs_function_execution"
            };
            let request_module = input
                .get("target")
                .and_then(|target| target.get("module"))
                .and_then(Value::as_str)
                .map(str::to_owned);
            let request_function = input
                .get("target")
                .and_then(|target| target.get("function"))
                .and_then(Value::as_str)
                .map(str::to_owned);
            let request_step_budget = input.get("step_budget").and_then(Value::as_u64);
            trace_phase(
                "request_json_decode",
                parse_started,
                json!({
                    "request_kind": request_kind,
                    "request_bytes": line.len(),
                    "target_module": request_module,
                    "target_function": request_function
                }),
            );
            let dispatch_started = Instant::now();
            trace_event(json!({
                "event": "phase_begin",
                "phase": "request_execution",
                "request_kind": request_kind,
                "target_module": request_module,
                "target_function": request_function,
                "step_budget": request_step_budget
            }));
            let output = if input.get("execution_status").is_some() {
                json!({
                    "backend": backend_name,
                    "modules": backend_sessions.len(),
                    "retained_sessions": backend_sessions.values().filter(|session| session.reused()).count(),
                })
            } else if let Some(module) = input.get("record_types").and_then(Value::as_str) {
                if let Some(record_types) = record_types_cache.get(module) {
                    record_types.clone()
                } else {
                    let (record_types, function_count) = {
                        let program = programs
                            .get(module)
                            .unwrap_or_else(|| panic!("record_types for unknown module {module}"));
                        (
                            serde_json::to_value(&program.record_types).unwrap(),
                            program.functions.len(),
                        )
                    };
                    if backend_sessions.contains_key(module) {
                        record_types_cache.insert(module.to_owned(), record_types.clone());
                        programs.remove(module);
                        trace_event(json!({
                            "event": "program_release",
                            "module": module,
                            "reason": "backend session is admitted and record identities are retained",
                            "function_count": function_count
                        }));
                    }
                    record_types
                }
            } else {
                let request: ExecutionRequest = serde_json::from_value(input).unwrap();
                let session = backend_sessions
                    .get(&request.target.module)
                    .unwrap_or_else(|| {
                        panic!(
                            "program-release profile requires an admitted backend for {}",
                            request.target.module
                        )
                    });
                json!(session.execute(&request))
            };
            trace_phase(
                "request_execution",
                dispatch_started,
                json!({
                    "request_kind": request_kind,
                    "target_module": request_module,
                    "target_function": request_function,
                    "step_budget": request_step_budget,
                    "reported_steps": output.get("steps"),
                    "reported_status": output.get("status"),
                    "reported_valid": output.get("valid"),
                    "reported_steps_semantics": match backend_name.as_deref() {
                        Some("research-bytecode") => "research-bytecode execution budget units",
                        Some("canonical-vm") => "canonical VM reported steps",
                        Some("cranelift") => "Cranelift does not report VM instruction steps",
                        _ => "not an instruction-count metric"
                    }
                }),
            );
            if telemetry_enabled() {
                trace_event(json!({
                    "event": "process_cost_report",
                    "report": mncs_model::cost_report()
                }));
            }
            let encode_started = Instant::now();
            trace_event(json!({
                "event": "phase_begin",
                "phase": "response_json_encode",
                "request_kind": request_kind
            }));
            let output_line = serde_json::to_string(&output).unwrap();
            trace_phase(
                "response_json_encode",
                encode_started,
                json!({"request_kind": request_kind, "response_bytes": output_line.len()}),
            );
            let write_started = Instant::now();
            trace_event(json!({
                "event": "phase_begin",
                "phase": "response_write",
                "request_kind": request_kind,
                "response_bytes": output_line.len()
            }));
            let stdout = io::stdout();
            let mut stdout = stdout.lock();
            writeln!(stdout, "{output_line}").unwrap();
            trace_phase(
                "response_write",
                write_started,
                json!({"request_kind": request_kind, "response_bytes": output_line.len()}),
            );
        }
        return;
    }
    let mut sessions: BTreeMap<String, BodyExecutionSession<'_>> = BTreeMap::new();
    for line in io::stdin().lock().lines() {
        // Local test transport over a pipe: requests carry whole native
        // Functions (deeply nested wire values), so the untrusted-input
        // recursion cap does not apply. Depth stays proportional to the
        // bounded fixture sources on this loop; never expose it to a
        // socket, and reach for serde_stacker before sending large
        // real-module Functions through verify-style requests.
        let line = line.unwrap();
        let parse_started = Instant::now();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "request_json_decode",
            "request_bytes": line.len()
        }));
        let mut deserializer = serde_json::Deserializer::from_str(&line);
        deserializer.disable_recursion_limit();
        let input: Value = serde::de::Deserialize::deserialize(&mut deserializer).unwrap();
        let request_kind = if input.get("execution_status").is_some() {
            "execution_status"
        } else if input.get("record_types").is_some() {
            "record_types"
        } else if input.get("project_oracle").is_some() {
            "project_oracle"
        } else if input.get("emit_vm_artifact").is_some() {
            "emit_vm_artifact"
        } else if input.get("elaborate").is_some() {
            "elaborate"
        } else if input.get("ssa").is_some() {
            "ssa_oracle"
        } else {
            "mncs_function_execution"
        };
        let request_module = input
            .get("target")
            .and_then(|target| target.get("module"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let request_function = input
            .get("target")
            .and_then(|target| target.get("function"))
            .and_then(Value::as_str)
            .map(str::to_owned);
        let request_step_budget = input.get("step_budget").and_then(Value::as_u64);
        trace_phase(
            "request_json_decode",
            parse_started,
            json!({
                "request_kind": request_kind,
                "request_bytes": line.len(),
                "target_module": request_module,
                "target_function": request_function
            }),
        );
        let dispatch_started = Instant::now();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "request_execution",
            "request_kind": request_kind,
            "target_module": request_module,
            "target_function": request_function,
            "step_budget": request_step_budget
        }));
        let output = if let Some(text) = input.get("oracle").and_then(Value::as_str) {
            serde_json::to_value(mncs_syntax::parse(&envelope(text.to_owned()))).unwrap()
        } else if input.get("execution_status").is_some() {
            json!({
                "backend": backend_name,
                "modules": backend_sessions.len(),
                "retained_sessions": backend_sessions.values().filter(|session| session.reused()).count(),
            })
        } else if let Some(module) = input.get("record_types").and_then(Value::as_str) {
            json!(programs[module].record_types)
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
            } else if let Some(session) = sessions.get(&request.target.module) {
                json!(session.execute(&request))
            } else {
                let module = request.target.module.clone();
                let program = programs
                    .get(&module)
                    .unwrap_or_else(|| panic!("no retained backend or program for {module}"));
                sessions.insert(module.clone(), BodyExecutionSession::new(program));
                json!(sessions[&module].execute(&request))
            }
        };
        trace_phase(
            "request_execution",
            dispatch_started,
            json!({
                "request_kind": request_kind,
                "target_module": request_module,
                "target_function": request_function,
                "step_budget": request_step_budget,
                "reported_steps": output.get("steps"),
                "reported_steps_semantics": match backend_name.as_deref() {
                    Some("research-bytecode") => "research-bytecode execution budget units",
                    Some("canonical-vm") => "canonical VM reported steps",
                    Some("cranelift") => "not an instruction-count metric",
                    _ => "executor-specific; UNKNOWN",
                },
                "reported_status": output.get("status"),
                "reported_valid": output.get("valid")
            }),
        );
        if telemetry_enabled() {
            trace_event(json!({
                "event": "process_cost_report",
                "report": mncs_model::cost_report()
            }));
        }
        let encode_started = Instant::now();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "response_json_encode",
            "request_kind": request_kind
        }));
        let output_line = serde_json::to_string(&output).unwrap();
        trace_phase(
            "response_json_encode",
            encode_started,
            json!({"request_kind": request_kind, "response_bytes": output_line.len()}),
        );
        let write_started = Instant::now();
        trace_event(json!({
            "event": "phase_begin",
            "phase": "response_write",
            "request_kind": request_kind,
            "response_bytes": output_line.len()
        }));
        let stdout = io::stdout();
        let mut stdout = stdout.lock();
        writeln!(stdout, "{output_line}").unwrap();
        stdout.flush().unwrap();
        drop(stdout);
        trace_phase(
            "response_write",
            write_started,
            json!({"request_kind": request_kind, "response_bytes": output_line.len()}),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mncs_model::{
        BackendArtifact, BackendIdentity, CompilerArtifactRef, ExecutionTypeArgument,
        TransformationStatus,
    };

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

    fn test_seed(module: &str, function: &str, value: u32) -> HostGenericSeedRequest {
        HostGenericSeedRequest::from_request(
            module,
            function,
            &[ExecutionTypeArgument::Nat { value }],
        )
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
    fn frontend_cache_is_backend_independent_and_module_seed_scoped() {
        let cranelift = test_toolchain(Some("cranelift"));
        let research = test_toolchain(Some("research-bytecode"));
        let closure = test_closure();
        let module_seed = test_seed("mncs.compiler.project.v1", "compile_project_target", 1024);
        let unrelated_seed = test_seed("mncs.compiler.lexer.v1", "lex", 64);
        let changed_module_seed =
            test_seed("mncs.compiler.project.v1", "compile_project_target", 512);
        let only_module = module_seeds_for("mncs.compiler.project.v1", &[module_seed.clone()]);
        let with_unrelated = module_seeds_for(
            "mncs.compiler.project.v1",
            &[module_seed.clone(), unrelated_seed],
        );
        let changed = module_seeds_for("mncs.compiler.project.v1", &[changed_module_seed]);

        assert_eq!(only_module, with_unrelated);
        assert_ne!(only_module, changed);
        assert_ne!(
            cache_key_hex(
                &cranelift,
                "mncs.compiler.project.v1",
                &closure,
                "same-seeds"
            ),
            cache_key_hex(
                &research,
                "mncs.compiler.project.v1",
                &closure,
                "same-seeds"
            ),
            "backend artifact identities must remain distinct",
        );
        assert_eq!(
            frontend_cache_key_hex(
                &cranelift,
                "mncs.compiler.project.v1",
                &closure,
                &only_module,
            ),
            frontend_cache_key_hex(
                &research,
                "mncs.compiler.project.v1",
                &closure,
                &with_unrelated,
            ),
            "backend and unrelated module seeds must not invalidate a frontend Program",
        );
        assert_ne!(
            frontend_cache_key_hex(
                &cranelift,
                "mncs.compiler.project.v1",
                &closure,
                &only_module,
            ),
            frontend_cache_key_hex(&cranelift, "mncs.compiler.project.v1", &closure, &changed,),
            "the target module's own specialization must invalidate its Program",
        );
    }

    #[test]
    fn frontend_program_entry_loads_under_each_backend_identity() {
        let cranelift = frontend_toolchain_identity(&test_toolchain(Some("cranelift")));
        let research = frontend_toolchain_identity(&test_toolchain(Some("research-bytecode")));
        let program = tiny_program();
        let entry = serde_json::to_vec(&serde_json::json!({
            "identity": cranelift,
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        let (loaded, artifact) =
            parse_cache_entry(&entry, &research, None).expect("backend-independent Program hit");
        assert!(artifact.is_none());
        assert_eq!(loaded.module, program.module);
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
        // A completed frontend Program remains reusable when its backend
        // compile did not produce an artifact.
        let interrupted_backend = serde_json::to_vec(&serde_json::json!({
            "identity": backended,
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        let (loaded, artifact) =
            parse_cache_entry(&interrupted_backend, &backended, Some("cranelift"))
                .expect("frontend-only backend cache hit");
        assert!(artifact.is_none());
        assert_eq!(loaded.module, program.module);
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
        let frontend_only = serde_json::to_vec(&serde_json::json!({
            "identity": backended,
            "program": program,
            "artifact": serde_json::Value::Null,
        }))
        .unwrap();
        assert!(
            parse_cache_entry(&frontend_only, &backended, Some("cranelift"))
                .is_some_and(|(_, artifact)| artifact.is_none())
        );
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
