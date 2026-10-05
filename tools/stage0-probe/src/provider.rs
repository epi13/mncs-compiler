//! Provider transport into the existing pinned compiler and direct emitter.
//! No compiler policy or execution semantics are reimplemented here.
use super::*;
use mncs_compiler::ModuleResolutionOutcome;
use std::{
    cell::RefCell,
    path::{Path, PathBuf},
};

pub fn producer() -> Value {
    serde_json::from_str(include_str!(concat!(env!("OUT_DIR"), "/producer.json"))).unwrap()
}
#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    schema_version: String,
    source: PathBuf,
    logical_name: String,
    #[serde(default)]
    libraries: Vec<PathBuf>,
    #[serde(default)]
    seeds: Vec<HostGenericSeedRequest>,
    #[serde(default)]
    include_tests: bool,
    #[serde(default)]
    calls: Vec<ExecutionRequest>,
}
struct Resolver {
    roots: Vec<PathBuf>,
    observed: RefCell<BTreeMap<String, String>>,
}
impl Resolver {
    fn candidates(root: &Path, module: &str) -> Vec<PathBuf> {
        let mut names = vec![module.to_owned()];
        if let Some((head, tail)) = module.rsplit_once('.') {
            if tail
                .strip_prefix('v')
                .is_some_and(|v| !v.is_empty() && v.chars().all(|c| c.is_ascii_digit()))
            {
                names.push(head.to_owned());
            }
        }
        let mut paths = Vec::new();
        for name in names {
            paths.push(root.join(format!("{}.mncs", name.rsplit('.').next().unwrap())));
            paths.push(root.join(format!("{}.mncs", name.replace('.', "/"))));
            if let Some(name) = name.strip_prefix("mncs.") {
                paths.push(root.join(format!("{}.mncs", name.replace('.', "/"))));
            }
        }
        paths.push(root.join(format!("{}.mncs", module.rsplit('.').next().unwrap())));
        paths
    }
}
impl ModuleResolver for Resolver {
    fn resolve(&self, module: &str) -> Option<SourceEnvelope> {
        match self.resolve_detailed(module) {
            ModuleResolutionOutcome::Resolved(e) => Some(*e),
            _ => None,
        }
    }
    fn resolve_detailed(&self, module: &str) -> ModuleResolutionOutcome {
        if module.is_empty()
            || module.contains("..")
            || !module
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '_')
        {
            return ModuleResolutionOutcome::NotFound;
        }
        let mut authorities: BTreeMap<String, PathBuf> = BTreeMap::new();
        for root in &self.roots {
            for path in Self::candidates(root, module) {
                let Ok(path) = path.canonicalize() else {
                    continue;
                };
                if !path.starts_with(root) {
                    continue;
                }
                let Ok(text) = std::fs::read_to_string(&path) else {
                    continue;
                };
                if mncs_syntax::declared_module_name(&text)
                    .is_some_and(|name| mncs_syntax::module_names_compatible(module, &name))
                {
                    authorities.entry(text).or_insert(path);
                }
            }
        }
        match authorities.len() {
            0 => ModuleResolutionOutcome::NotFound,
            1 => {
                let (text, path) = authorities.into_iter().next().unwrap();
                self.observed.borrow_mut().insert(
                    path.to_string_lossy().to_string(),
                    mncs_model::sha256_hex(text.as_bytes()),
                );
                ModuleResolutionOutcome::Resolved(Box::new(SourceEnvelope::inline(
                    SourceArtifactKind::Program,
                    module,
                    text,
                )))
            }
            _ => ModuleResolutionOutcome::Conflict(
                authorities
                    .into_values()
                    .map(|p| p.display().to_string())
                    .collect(),
            ),
        }
    }
}
pub fn run(path: &str) -> Result<Value, String> {
    let request: Request = serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    if request.schema_version != "mncs.compiler-vm-request/1" || request.logical_name.is_empty() {
        return Err("unsupported request schema or empty logical_name".into());
    }
    let source = request.source.canonicalize().map_err(|e| e.to_string())?;
    let mut roots = vec![source.parent().unwrap().to_path_buf()];
    for root in request.libraries {
        roots.push(root.canonicalize().map_err(|e| e.to_string())?);
    }
    let text = std::fs::read_to_string(&source).map_err(|e| e.to_string())?;
    let resolver = Resolver {
        roots,
        observed: RefCell::new(BTreeMap::from([(
            source.display().to_string(),
            mncs_model::sha256_hex(text.as_bytes()),
        )])),
    };
    let compiler = ReferenceCompiler::default();
    let mut front = compiler.front_end_with_resolver_and_seeds(
        SourceEnvelope::inline(SourceArtifactKind::Program, request.logical_name, text),
        &resolver,
        &request.seeds,
    );
    if !front.is_valid() {
        return Ok(json!({"status":"invalid_source", "diagnostics":front.diagnostics}));
    }
    let program = front.program.take().unwrap();
    let selected = if request.include_tests {
        program
    } else {
        program.without_tests()
    };
    let producer = producer();
    let (artifact, source_map) = vm_emit::emit_vm_artifact_with_source_map(
        &compiler,
        &selected,
        Some(producer["identity"].as_str().unwrap()),
        front.execution_source_map.take(),
    )?;
    let oracle = BodyExecutionSession::new(&selected);
    let results: Vec<_> = request.calls.iter().map(|r| oracle.execute(r)).collect();
    Ok(
        json!({"status":"emitted", "module":selected.module, "artifact":artifact, "source_map":source_map, "test_inventory":front.test_inventory, "module_resolutions":front.module_resolutions, "callable_bindings":mncs_codegen::language_owned_callable_bindings(&selected), "source_inputs":resolver.observed.into_inner(), "producer":producer, "reference_results":results}),
    )
}
