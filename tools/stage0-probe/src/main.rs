//! Temporary test transport only. All compiler behavior comes from the pinned
//! reference libraries or the MNCS source under test. No replacement semantics.
use mncs_codegen::OwnedExecutionSession;
use mncs_compiler::{ModuleResolver, NullResolver, ReferenceCompiler};
use mncs_model::{
    ArtifactRepresentation, BodyExecutionSession, ExecutionRequest, HostGenericSeedRequest,
};
use mncs_syntax::{SourceArtifactKind, SourceEnvelope};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, BufRead},
};

struct Sources(BTreeMap<String, SourceEnvelope>);
impl ModuleResolver for Sources {
    fn resolve(&self, name: &str) -> Option<SourceEnvelope> {
        self.0.get(name).cloned()
    }
}
fn envelope(text: String) -> SourceEnvelope {
    SourceEnvelope::inline(SourceArtifactKind::Program, "probe", text)
}
fn main() {
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
    let execution_modules: Option<Vec<String>> = std::env::var("MNCS_PROBE_EXECUTION_MODULES").ok().map(|raw| {
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
    let include_flow = wanted
        .as_ref()
        .is_some_and(|names| names.iter().any(|name| matches!(name.as_str(), "flow" | "ssa" | "project")));
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
    // artifacts keyed by every input that determines them (backend,
    // module closure bytes, seed request, module selection). A hit
    // skips elaboration and lowering entirely; invalidation is exact
    // because the key is the content. Unset MNCS_PROBE_CACHE_DIR
    // disables the cache (historical behavior).
    let cache_dir = std::env::var("MNCS_PROBE_CACHE_DIR")
        .ok()
        .filter(|dir| !dir.is_empty());
    let seeds_raw = std::env::var("MNCS_PROBE_GENERIC_SEEDS").unwrap_or_default();
    let mut cache_hits = 0u32;
    let mut cache_misses = 0u32;
    let cache_key = |module: &str| -> String {
        let mut input = String::from("probe-cache-v1\n");
        input.push_str(backend_name.as_deref().unwrap_or("none"));
        input.push('\n');
        input.push_str(module);
        input.push('\n');
        let mut names: Vec<_> = sources.0.keys().collect();
        names.sort();
        for name in names {
            input.push_str(name);
            input.push('\n');
        }
        // Source bytes: re-read the loaded leaves (kilobytes; the
        // envelopes already hold them but do not lend the text back).
        for file in [
            "source", "lexer", "parser", "kernel", "segment", "decl", "flow", "ssa", "project",
        ] {
            let name = format!("mncs.compiler.{file}.v1");
            if sources.0.contains_key(&name) {
                let text =
                    std::fs::read_to_string(format!("src/compiler/{file}.mncs")).unwrap();
                input.push_str(&text);
                input.push('\n');
            }
        }
        input.push_str(&seeds_raw);
        mncs_model::sha256_hex(input.as_bytes())
    };
    // Entries are gzip-compressed JSON (`{program, artifact|null}`) via
    // the system gzip: payloads shrink ~25x (hundreds of MB to tens)
    // with no new dependencies. Any compression failure degrades to a
    // miss, never an error.
    let cache_load = |key: &str| -> Option<(mncs_model::Program, Option<mncs_model::BackendArtifact>)> {
        let dir = cache_dir.as_ref()?;
        let output = std::process::Command::new("gzip")
            .args(["-dc", &format!("{dir}/{key}.json.gz")])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let entry: serde_json::Value = serde_json::from_slice(&output.stdout).ok()?;
        let program =
            serde_json::from_value::<mncs_model::Program>(entry.get("program")?.clone()).ok()?;
        if backend_name.is_none() {
            return Some((program, None));
        }
        let artifact = serde_json::from_value::<mncs_model::BackendArtifact>(
            entry.get("artifact")?.clone(),
        )
        .ok()?;
        if !artifact.identity_is_valid() {
            return None;
        }
        Some((program, Some(artifact)))
    };
    let cache_store = |key: &str, program: &mncs_model::Program, artifact: Option<&mncs_model::BackendArtifact>| {
        let Some(dir) = cache_dir.as_ref() else {
            return;
        };
        if std::fs::create_dir_all(dir).is_err() {
            return;
        }
        let entry = serde_json::json!({"program": program, "artifact": artifact});
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
        if backend_name.is_none() {
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
                        .unwrap_or_else(|| panic!("{backend} emitted no artifact for {name}: {result:?}"))
                        .clone();
                    cache_store(&cache_key(name), program, Some(&artifact));
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
    if let Some(backend) = backend_name.as_deref() {
        let reused = backend_sessions.values().filter(|session| session.reused()).count();
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
        eprintln!("mncs-stage0-probe backend={backend} modules={} retained_sessions={reused} artifacts={artifacts:?} cache_hits={cache_hits} cache_misses={cache_misses} ready_s={:.1}", backend_sessions.len(), t_start.elapsed().as_secs_f64());
    } else if cache_dir.is_some() {
        eprintln!("mncs-stage0-probe backend=none modules={} cache_hits={cache_hits} cache_misses={cache_misses}", programs.len());
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
                imported.insert(module.clone(), envelope(text.as_str().expect("module source").to_owned()));
            }
            let resolver = Sources(imported);
            let result = ReferenceCompiler::default()
                .front_end_with_resolver(envelope(root.to_owned()), &resolver);
            let ssa = result.program.as_ref().and_then(|program| program.lower_to_ssa().ok());
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
            let program = result.program.as_ref().map(|program| json!({
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
            }));
            json!({
                "valid": result.is_valid(),
                "diagnostics": result.diagnostics,
                "module_resolutions": result.module_resolutions,
                "program": program,
                "ssa": ssa,
                "executions": executions,
            })
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
