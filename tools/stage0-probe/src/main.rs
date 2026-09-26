//! Temporary test transport only. All compiler behavior comes from the pinned
//! reference libraries or the MNCS source under test. No replacement semantics.
use mncs_compiler::{ModuleResolver, NullResolver, ReferenceCompiler};
use mncs_model::{BodyExecutionSession, ExecutionRequest};
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
        "source", "lexer", "parser", "kernel", "segment", "decl", "flow",
    ] {
        if file == "flow" && !include_flow {
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
        let result = ReferenceCompiler::default().front_end_with_resolver(source.clone(), &sources);
        assert!(result.is_valid(), "{name}: {:?}", result.diagnostics);
        programs.insert(name.clone(), result.program.unwrap());
    }
    let sessions: BTreeMap<_, _> = programs
        .iter()
        .map(|(name, program)| (name.clone(), BodyExecutionSession::new(program)))
        .collect();
    for line in io::stdin().lock().lines() {
        let input: Value = serde_json::from_str(&line.unwrap()).unwrap();
        let output = if let Some(text) = input.get("oracle").and_then(Value::as_str) {
            serde_json::to_value(mncs_syntax::parse(&envelope(text.to_owned()))).unwrap()
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
            let session = &sessions[&request.target.module];
            json!(session.execute(&request))
        };
        println!("{}", output);
    }
}
