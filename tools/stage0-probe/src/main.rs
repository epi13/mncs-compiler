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
        .is_some_and(|names| names.iter().any(|name| name == "flow"));
    let mut sources = Sources(BTreeMap::new());
    for file in [
        "source", "lexer", "parser", "kernel", "segment", "decl", "flow", "project",
    ] {
        if matches!(file, "flow" | "project") && !include_flow {
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
    let mut programs = BTreeMap::new();
    for (name, source) in &sources.0 {
        if execution_modules
            .as_ref()
            .is_some_and(|modules| !modules.iter().any(|module| module == name))
        {
            continue;
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
        programs.insert(name.clone(), result.program.unwrap());
    }
    let sessions: BTreeMap<_, _> = programs
        .iter()
        .map(|(name, program)| (name.clone(), BodyExecutionSession::new(program)))
        .collect();
    // Optional development path: compile the exact loaded reference Program
    // once, retain its backend session, then answer the same requests through
    // that executable artifact. The ordinary interpreter remains available
    // as the independent comparison path.
    let backend_name = std::env::var("MNCS_PROBE_BACKEND").ok();
    let backend_artifacts: BTreeMap<_, _> = backend_name
        .as_deref()
        .map(|backend| {
            let compiler = ReferenceCompiler::default();
            programs
                .iter()
                .map(|(name, program)| {
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
        eprintln!("mncs-stage0-probe backend={backend} modules={} retained_sessions={reused} artifacts={artifacts:?}", backend_sessions.len());
    }
    for line in io::stdin().lock().lines() {
        let input: Value = serde_json::from_str(&line.unwrap()).unwrap();
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
