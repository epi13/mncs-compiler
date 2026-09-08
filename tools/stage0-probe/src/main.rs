//! Temporary test transport only. All compiler behavior comes from the pinned
//! reference libraries or the MNCS source under test. No replacement semantics.
use mncs_compiler::{ModuleResolver, ReferenceCompiler};
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
    let mut sources = Sources(BTreeMap::new());
    for file in ["source", "lexer", "parser", "kernel"] {
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
        } else {
            let request: ExecutionRequest = serde_json::from_value(input).unwrap();
            let session = &sessions[&request.target.module];
            json!(session.execute(&request))
        };
        println!("{}", output);
    }
}
