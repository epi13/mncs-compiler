use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::PathBuf,
};

use mncs_model::{
    BodyType, ExecutionRequest, ExecutionValue, SemanticId, SequenceBound, SsaModule,
    MAX_SEQUENCE_BOUND,
};
use mncs_vm::{
    admit::admit_owned,
    capability::{CapabilityEnv, EffectRequest, EffectResponse, Provider},
    outcome::Outcome,
    session::{CallSpec, Session},
    value::{from_wire, to_wire, Value},
};

const PROVIDER_ID: &str = "mncs.compiler.test-structured-file-provider/1";
const INPUT_SCHEMA: &str = "mncs.test-digest-request/1";
const PROVIDER_REQUEST_SCHEMA: &str = "mncs.test-provider-request/1";
const OUTPUT_SCHEMA: &str = "mncs.test-digest/1";

struct StructuredFileProvider {
    capability: String,
    input_path: PathBuf,
    output_path: PathBuf,
    fail_write: bool,
    allowed_operations: BTreeSet<String>,
    ssa_module: SsaModule,
}

#[derive(Debug, Clone)]
struct DispatchBinding {
    authorized_identity: String,
    dispatch_alias: String,
    mapped_host_calls: usize,
    operations: BTreeSet<String>,
    call_authority_identities: Vec<String>,
}

impl StructuredFileProvider {
    fn provider_failure(&self, detail: impl Into<String>) -> Outcome {
        Outcome::ProviderFailure {
            provider: PROVIDER_ID.to_owned(),
            detail: detail.into(),
        }
    }

    fn response(&self, outputs: Vec<Value>) -> EffectResponse {
        EffectResponse {
            provider: PROVIDER_ID.to_owned(),
            outputs,
            note: "bounded temporary structured file transport".to_owned(),
        }
    }
}

impl Provider for StructuredFileProvider {
    fn identity(&self) -> &str {
        PROVIDER_ID
    }

    fn invoke(&self, request: &EffectRequest) -> Result<EffectResponse, Outcome> {
        if request.capability != self.capability {
            return Err(self.provider_failure("capability identity mismatch"));
        }
        if !self.allowed_operations.contains(&request.operation) {
            return Err(self.provider_failure(format!(
                "operation {} is not bound to this dispatch alias",
                request.operation
            )));
        }
        match request.operation.as_str() {
            "structured_read" => {
                if request.inputs.len() != 2 {
                    return Err(self.provider_failure("structured_read arity mismatch"));
                }
                let path = value_bytes(&request.inputs[0])
                    .and_then(|bytes| String::from_utf8(bytes).map_err(|e| e.to_string()))
                    .map_err(|e| self.provider_failure(format!("bad read path: {e}")))?;
                if PathBuf::from(path) != self.input_path {
                    return Err(self.provider_failure("read path is outside the admitted fixture"));
                }
                let schema = value_bytes(&request.inputs[1])
                    .and_then(|bytes| String::from_utf8(bytes).map_err(|e| e.to_string()))
                    .map_err(|e| self.provider_failure(format!("bad read schema: {e}")))?;
                if schema != INPUT_SCHEMA && schema != PROVIDER_REQUEST_SCHEMA {
                    return Err(self.provider_failure("unexpected structured read schema"));
                }
                let bytes = fs::read(&self.input_path)
                    .map_err(|e| self.provider_failure(format!("read input: {e}")))?;
                let wire: ExecutionValue = serde_json::from_slice(&bytes)
                    .map_err(|e| self.provider_failure(format!("decode input: {e}")))?;
                Ok(self.response(vec![from_wire(&wire)]))
            }
            "structured_write" => {
                if request.inputs.len() != 3 {
                    return Err(self.provider_failure("structured_write arity mismatch"));
                }
                let path = value_bytes(&request.inputs[0])
                    .and_then(|bytes| String::from_utf8(bytes).map_err(|e| e.to_string()))
                    .map_err(|e| self.provider_failure(format!("bad write path: {e}")))?;
                if PathBuf::from(path) != self.output_path {
                    return Err(self.provider_failure("write path is outside the admitted fixture"));
                }
                let schema = value_bytes(&request.inputs[1])
                    .and_then(|bytes| String::from_utf8(bytes).map_err(|e| e.to_string()))
                    .map_err(|e| self.provider_failure(format!("bad write schema: {e}")))?;
                if schema != OUTPUT_SCHEMA {
                    return Err(self.provider_failure("unexpected structured write schema"));
                }
                if self.fail_write {
                    return Ok(self.response(vec![Value::Integer {
                        value: 0,
                        bits: 64,
                        signed: false,
                    }]));
                }
                let bytes = serde_json::to_vec(&to_wire(&request.inputs[2]))
                    .map_err(|e| self.provider_failure(format!("encode output: {e}")))?;
                fs::write(&self.output_path, bytes)
                    .map_err(|e| self.provider_failure(format!("write output: {e}")))?;
                Ok(self.response(vec![Value::Integer {
                    value: 1,
                    bits: 64,
                    signed: false,
                }]))
            }
            "structured_digest" => {
                if request.inputs.len() != 1 {
                    return Err(self.provider_failure("structured_digest arity mismatch"));
                }
                let operand = &request.inputs[0];
                let operand_type = infer_value_type(&self.ssa_module, operand)
                    .map_err(|error| self.provider_failure(error))?;
                let external = external_value(&self.ssa_module, &operand_type, operand, 0)
                    .map_err(|error| self.provider_failure(error))?;
                let canonical = serde_json::to_vec(&external).map_err(|error| {
                    self.provider_failure(format!("encode digest input: {error}"))
                })?;
                if canonical.len() > 64 * 1024 {
                    return Err(
                        self.provider_failure("structured digest input exceeds 65536 bytes")
                    );
                }
                let hex = mncs_model::sha256_hex(&canonical);
                let digest = (0..32)
                    .map(|index| {
                        u8::from_str_radix(&hex[index * 2..index * 2 + 2], 16)
                            .expect("sha256_hex always returns lowercase hexadecimal")
                    })
                    .map(Value::Byte)
                    .collect();
                Ok(self.response(vec![Value::Sequence {
                    elements: std::sync::Arc::new(digest),
                }]))
            }
            operation => Err(self.provider_failure(format!("unsupported operation {operation}"))),
        }
    }
}

fn value_bytes(value: &Value) -> Result<Vec<u8>, String> {
    let Value::Sequence { elements } = value else {
        return Err("expected a byte sequence".to_owned());
    };
    elements
        .iter()
        .map(|item| match item {
            Value::Byte(byte) => Ok(*byte),
            _ => Err("sequence contains a non-byte value".to_owned()),
        })
        .collect()
}

fn infer_value_type(module: &SsaModule, value: &Value) -> Result<BodyType, String> {
    match value {
        Value::Record { type_id, name, .. } => {
            let declaration = module
                .record_types
                .iter()
                .find(|record| record.identity.0 == *type_id && record.name == *name)
                .ok_or_else(|| {
                    format!("structured digest has no SSA record declaration for {name}")
                })?;
            Ok(BodyType::Record {
                identity: declaration.identity.clone(),
                name: declaration.name.clone(),
            })
        }
        Value::Finite { type_id, .. } => Ok(BodyType::Finite {
            identity: SemanticId(type_id.clone()),
            name: type_id.rsplit("::").next().unwrap_or(type_id).to_owned(),
        }),
        Value::Sequence { elements } => {
            let first = elements.first().ok_or_else(|| {
                "structured digest cannot infer the type of an empty top-level sequence".to_owned()
            })?;
            Ok(BodyType::Sequence {
                element: Box::new(infer_value_type(module, first)?),
                bound: SequenceBound::UpTo(MAX_SEQUENCE_BOUND),
            })
        }
        Value::Integer { bits, signed, .. } if (1..=u16::MAX as u32).contains(bits) => {
            Ok(BodyType::Integer(mncs_model::IntegerType {
                bits: *bits as u16,
                signed: *signed,
            }))
        }
        Value::Boolean(_) => Ok(BodyType::Bool),
        Value::Byte(_) => Ok(BodyType::Byte),
        Value::FloatBits(_) => Ok(BodyType::Float(mncs_model::FloatType::f64())),
        Value::Vector { .. } => {
            Err("structured digest cannot infer an untyped top-level vector".to_owned())
        }
        Value::Mask { .. } => {
            Err("structured digest cannot infer an untyped top-level mask".to_owned())
        }
        Value::Integer { bits, .. } => Err(format!("invalid structured integer width {bits}")),
    }
}

fn external_value(
    module: &SsaModule,
    expected: &BodyType,
    value: &Value,
    depth: usize,
) -> Result<serde_json::Value, String> {
    if depth > 64 {
        return Err("structured digest input exceeds the nesting bound".to_owned());
    }
    match (expected, value) {
        (BodyType::Bool, Value::Boolean(value)) => Ok(serde_json::Value::Bool(*value)),
        (BodyType::Integer(expected), Value::Integer { value, bits, signed })
            if expected.bits == *bits as u16 && expected.signed == *signed =>
        {
            serde_json::Number::from_i128(*value)
                .map(serde_json::Value::Number)
                .ok_or_else(|| "typed integer cannot be represented as JSON".to_owned())
        }
        (BodyType::Float(_), Value::FloatBits(bits)) => {
            serde_json::Number::from_f64(f64::from_bits(*bits))
                .map(serde_json::Value::Number)
                .ok_or_else(|| "typed float is not a finite JSON number".to_owned())
        }
        (BodyType::Byte, Value::Byte(value)) => {
            Ok(serde_json::Value::Number(serde_json::Number::from(*value)))
        }
        (BodyType::Sequence { element, bound }, Value::Sequence { elements }) => {
            match bound {
                SequenceBound::Exact(expected) if elements.len() != *expected as usize => {
                    return Err("typed sequence does not match its exact bound".to_owned());
                }
                SequenceBound::UpTo(maximum) if elements.len() > *maximum as usize => {
                    return Err("typed sequence exceeds its declared bound".to_owned());
                }
                SequenceBound::Param(_) | SequenceBound::UpToParam(_) => {
                    return Err("structured digest refuses unresolved sequence bounds".to_owned());
                }
                _ => {}
            }
            if **element == BodyType::Byte {
                let bytes = elements
                    .iter()
                    .map(|item| match item {
                        Value::Byte(byte) => Ok(*byte),
                        _ => Err("typed byte sequence contains a non-byte value".to_owned()),
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                if *bound == SequenceBound::Exact(32) {
                    return Ok(serde_json::Value::String(
                        bytes.iter().map(|byte| format!("{byte:02x}")).collect(),
                    ));
                }
                return match String::from_utf8(bytes.clone()) {
                    Ok(text) => Ok(serde_json::Value::String(text)),
                    Err(_) => Ok(serde_json::Value::Array(
                        bytes
                            .into_iter()
                            .map(|byte| serde_json::Value::Number(serde_json::Number::from(byte)))
                            .collect(),
                    )),
                };
            }
            elements
                .iter()
                .map(|item| external_value(module, element, item, depth + 1))
                .collect::<Result<Vec<_>, _>>()
                .map(serde_json::Value::Array)
        }
        (BodyType::Record { identity, name }, Value::Record { type_id, name: value_name, fields })
            if identity.0 == *type_id && name == value_name =>
        {
            let declaration = module
                .record_types
                .iter()
                .find(|record| record.identity == *identity)
                .ok_or_else(|| format!("structured digest has no SSA record declaration for {name}"))?;
            if fields.len() != declaration.fields.len() {
                return Err(format!("typed record {name} does not match its declared fields"));
            }
            let mut object = serde_json::Map::new();
            for (field_name, field_type) in &declaration.fields {
                let field_value = fields
                    .iter()
                    .find(|(actual_name, _)| actual_name == field_name)
                    .map(|(_, actual_value)| actual_value)
                    .ok_or_else(|| format!("typed record {name} is missing field {field_name}"))?;
                object.insert(
                    field_name.clone(),
                    external_value(module, field_type, field_value, depth + 1)?,
                );
            }
            Ok(serde_json::Value::Object(object))
        }
        (BodyType::Finite { identity, .. }, Value::Finite { type_id, variant_id, payload, .. })
            if identity.0 == *type_id && payload.is_empty() =>
        {
            let variant = variant_id
                .rsplit("::")
                .next()
                .filter(|name| !name.is_empty())
                .ok_or_else(|| "typed finite variant has no external name".to_owned())?;
            Ok(serde_json::Value::String(variant.to_owned()))
        }
        (BodyType::Vector { .. }, Value::Vector { .. })
        | (BodyType::Mask { .. }, Value::Mask { .. }) => {
            Err("structured digest vector or mask projection is not implemented by this bounded provider".to_owned())
        }
        (BodyType::Named(name), _) => Err(format!("structured digest refuses unresolved type {name:?}")),
        (BodyType::GenericParam { name }, _) => {
            Err(format!("structured digest refuses unspecialized type {name:?}"))
        }
        _ => Err(format!("typed value does not satisfy {}", expected.semantic_name())),
    }
}

fn take_arg(args: &mut impl Iterator<Item = String>, label: &str) -> Result<String, String> {
    args.next().ok_or_else(|| format!("missing {label}"))
}

fn execute_call(
    session: &mut Session<'_>,
    request_path: &PathBuf,
    input_path: &PathBuf,
    output_path: &PathBuf,
    bindings: &[DispatchBinding],
    ssa_module: &SsaModule,
    mode: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    if mode != "none" && mode != "success" && mode != "fail-write" {
        return Err(std::io::Error::other(format!("unsupported provider mode {mode}")).into());
    }
    if mode != "none" && output_path.exists() {
        return Err(std::io::Error::other("output path already exists").into());
    }
    let request: ExecutionRequest = serde_json::from_slice(&fs::read(request_path)?)?;
    let spec = CallSpec::from_request(request, None).map_err(std::io::Error::other)?;
    let mut caps = CapabilityEnv::empty();
    if mode != "none" {
        for binding in bindings {
            let provider = StructuredFileProvider {
                capability: binding.dispatch_alias.clone(),
                input_path: input_path.clone(),
                output_path: output_path.clone(),
                fail_write: mode == "fail-write",
                allowed_operations: binding.operations.clone(),
                ssa_module: ssa_module.clone(),
            };
            caps = caps.bind(&binding.dispatch_alias, provider);
            for call_identity in &binding.call_authority_identities {
                caps = caps.bind(
                    call_identity,
                    StructuredFileProvider {
                        capability: binding.dispatch_alias.clone(),
                        input_path: input_path.clone(),
                        output_path: output_path.clone(),
                        fail_write: mode == "fail-write",
                        allowed_operations: binding.operations.clone(),
                        ssa_module: ssa_module.clone(),
                    },
                );
            }
        }
    }
    let (outcome, record) = session.call(&caps, spec);
    let returned_wire = record.returned.iter().map(to_wire).collect::<Vec<_>>();
    Ok(serde_json::json!({
        "outcome": outcome,
        "record": record,
        "returned_wire": returned_wire,
        "capability_binding": {
            "authorized_capability_identity": bindings.iter().map(|binding| binding.authorized_identity.as_str()).collect::<Vec<_>>().join(";"),
            "runtime_dispatch_alias": bindings.iter().map(|binding| binding.dispatch_alias.as_str()).collect::<Vec<_>>().join(";"),
            "contract_dispatch_alias": bindings.iter().map(|binding| binding.dispatch_alias.as_str()).collect::<Vec<_>>().join(";"),
            "mapped_host_call_count": bindings.iter().map(|binding| binding.mapped_host_calls).sum::<usize>(),
            "bindings": bindings.iter().map(|binding| serde_json::json!({
                "authorized_capability_identity": binding.authorized_identity,
                "runtime_dispatch_alias": binding.dispatch_alias,
                "mapped_host_call_count": binding.mapped_host_calls,
                "operations": binding.operations,
                "call_authority_identities": binding.call_authority_identities,
            })).collect::<Vec<_>>(),
        },
    }))
}

fn absolute_path(row: &serde_json::Value, key: &str) -> Result<PathBuf, String> {
    let value = row
        .get(key)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| format!("batch call is missing {key}"))?;
    let path = PathBuf::from(value);
    if !path.is_absolute() {
        return Err(format!("batch call {key} must be an absolute path"));
    }
    Ok(path)
}

fn run_single(
    args: &mut impl Iterator<Item = String>,
    admitted: &mncs_vm::admit::Admitted,
    authorized_identity: &str,
    declared_dispatch_alias: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let request_path = PathBuf::from(take_arg(args, "execution request path")?);
    let input_path = PathBuf::from(take_arg(args, "input path")?);
    let output_path = PathBuf::from(take_arg(args, "output path")?);
    let mode = take_arg(args, "provider mode")?;
    let bindings =
        capability_dispatch_bindings(admitted, authorized_identity, declared_dispatch_alias)
            .map_err(std::io::Error::other)?;
    let ssa_module = admitted
        .ssa_module()
        .ok_or_else(|| std::io::Error::other("admitted artifact does not contain selected SSA"))?;
    let mut session = Session::open(admitted);
    execute_call(
        &mut session,
        &request_path,
        &input_path,
        &output_path,
        &bindings,
        ssa_module,
        &mode,
    )
}

fn run_batch(
    args: &mut impl Iterator<Item = String>,
    admitted: &mncs_vm::admit::Admitted,
    authorized_identity: &str,
    declared_dispatch_alias: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let manifest_path = PathBuf::from(take_arg(args, "batch manifest path")?);
    let manifest: serde_json::Value = serde_json::from_slice(&fs::read(manifest_path)?)?;
    if manifest
        .get("schema_version")
        .and_then(serde_json::Value::as_str)
        != Some("mncs.compiler.native-application-batch-request/1")
    {
        return Err(std::io::Error::other("unsupported application batch request schema").into());
    }
    let rows = manifest
        .get("calls")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| std::io::Error::other("batch request calls must be an array"))?;
    if rows.is_empty() || rows.len() > 64 {
        return Err(std::io::Error::other("batch request must contain 1 to 64 calls").into());
    }

    let bindings =
        capability_dispatch_bindings(admitted, authorized_identity, declared_dispatch_alias)
            .map_err(std::io::Error::other)?;
    let ssa_module = admitted
        .ssa_module()
        .ok_or_else(|| std::io::Error::other("admitted artifact does not contain selected SSA"))?;

    let mut unique_paths = BTreeSet::new();
    let mut session = Session::open(admitted);
    let mut calls = Vec::with_capacity(rows.len());
    for (index, row) in rows.iter().enumerate() {
        let request_path = absolute_path(row, "request_path").map_err(std::io::Error::other)?;
        let input_path = absolute_path(row, "input_path").map_err(std::io::Error::other)?;
        let output_path = absolute_path(row, "output_path").map_err(std::io::Error::other)?;
        for path in [&request_path, &input_path, &output_path] {
            let key = path.to_string_lossy().into_owned();
            if !unique_paths.insert(key) {
                return Err(std::io::Error::other(
                    "batch calls must use distinct request, input, and output paths",
                )
                .into());
            }
        }
        let mode = row
            .get("provider_mode")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| std::io::Error::other("batch call is missing provider_mode"))?;
        let result = execute_call(
            &mut session,
            &request_path,
            &input_path,
            &output_path,
            &bindings,
            ssa_module,
            mode,
        )?;
        calls.push(serde_json::json!({
            "call_index": index,
            "result": result,
        }));
    }
    Ok(serde_json::json!({
        "schema_version": "mncs.compiler.native-application-batch-result/1",
        "artifact_id": admitted.artifact_id(),
        "runtime_id": format!("mncs-vm-runtime:{}", admitted.artifact_id()),
        "call_count": calls.len(),
        "calls": calls,
    }))
}

fn split_binding_values(value: &str, label: &str) -> Result<Vec<String>, String> {
    let values = value
        .split(';')
        .map(str::trim)
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if values.is_empty() || values.iter().any(String::is_empty) {
        return Err(format!(
            "{label} must contain nonempty semicolon-separated values"
        ));
    }
    let mut unique = BTreeSet::new();
    if values.iter().any(|value| !unique.insert(value.clone())) {
        return Err(format!("{label} contains duplicate values"));
    }
    Ok(values)
}

fn capability_dispatch_bindings(
    admitted: &mncs_vm::admit::Admitted,
    authorized_identities: &str,
    declared_dispatch_aliases: &str,
) -> Result<Vec<DispatchBinding>, String> {
    let authorized_identities =
        split_binding_values(authorized_identities, "authorized capability identities")?;
    let declared_dispatch_aliases =
        split_binding_values(declared_dispatch_aliases, "declared VM dispatch aliases")?;
    if authorized_identities.len() != declared_dispatch_aliases.len() {
        return Err(
            "authorized capability identity count differs from declared dispatch alias count"
                .to_owned(),
        );
    }
    let ssa = admitted
        .ssa_module()
        .ok_or_else(|| "admitted artifact does not contain selected SSA".to_owned())?;
    let mut identities_by_alias = std::collections::BTreeMap::<String, BTreeSet<String>>::new();
    let mut aliases_by_identity =
        std::collections::BTreeMap::<String, BTreeMap<String, usize>>::new();
    let mut operations_by_alias = std::collections::BTreeMap::<String, BTreeSet<String>>::new();
    let mut call_authorities_by_alias =
        std::collections::BTreeMap::<String, BTreeSet<String>>::new();
    let requirements = admitted
        .artifact
        .requirements
        .capabilities
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>();
    let mut host_call_count = 0usize;
    for instruction in ssa
        .functions
        .iter()
        .flat_map(|function| &function.blocks)
        .flat_map(|block| &block.instructions)
    {
        match &instruction.kind {
            mncs_model::SsaInstructionKind::HostCall {
                capability,
                operation,
            } => {
                host_call_count += 1;
                if instruction.capability_uses.len() != 1 {
                    return Err(format!(
                        "HostCall {capability:?} has {} canonical capability uses; expected exactly one",
                        instruction.capability_uses.len()
                    ));
                }
                let identity = instruction.capability_uses[0].capability.0.clone();
                if !requirements.contains(&identity) {
                    return Err(format!(
                        "HostCall {capability:?} uses capability identity {identity} absent from artifact requirements"
                    ));
                }
                identities_by_alias
                    .entry(capability.clone())
                    .or_default()
                    .insert(identity.clone());
                operations_by_alias
                    .entry(capability.clone())
                    .or_default()
                    .insert(operation.clone());
                *aliases_by_identity
                    .entry(identity)
                    .or_default()
                    .entry(capability.clone())
                    .or_default() += 1;
            }
            mncs_model::SsaInstructionKind::Call {
                required_capabilities,
                ..
            } => {
                for identity in required_capabilities {
                    if !requirements.contains(&identity.0) {
                        return Err(format!(
                            "Call uses capability identity {} absent from artifact requirements",
                            identity.0
                        ));
                    }
                    let alias = identity
                        .0
                        .rsplit("::")
                        .next()
                        .filter(|alias| !alias.is_empty())
                        .ok_or_else(|| {
                            format!(
                                "Call capability identity {} has no source alias",
                                identity.0
                            )
                        })?;
                    call_authorities_by_alias
                        .entry(alias.to_owned())
                        .or_default()
                        .insert(identity.0.clone());
                }
            }
            _ => {}
        }
    }
    if host_call_count == 0 {
        return Err("admitted SSA contains no HostCalls".to_owned());
    }
    let mut bindings = Vec::with_capacity(authorized_identities.len());
    for (identity, declared_alias) in authorized_identities.iter().zip(&declared_dispatch_aliases) {
        if !admitted
            .artifact
            .requirements
            .capabilities
            .iter()
            .any(|required| required == identity)
        {
            return Err(format!(
                "artifact does not require authorized capability {identity}"
            ));
        }
        let aliases = aliases_by_identity.get(identity).ok_or_else(|| {
            format!("expected HostCalls for authorized identity {identity}, found none")
        })?;
        if aliases.len() != 1 {
            return Err(format!(
                "authorized identity {identity} maps to {} runtime aliases",
                aliases.len()
            ));
        }
        let (actual_alias, mapped_host_calls) = aliases.iter().next().expect("alias count checked");
        if actual_alias != declared_alias {
            return Err("application contract dispatch alias differs from admitted SSA".to_owned());
        }
        let identities = identities_by_alias
            .get(actual_alias)
            .expect("runtime alias collected");
        if identities.len() != 1 || !identities.contains(identity) {
            return Err(format!(
                "runtime alias {actual_alias:?} maps to multiple canonical capability identities"
            ));
        }
        bindings.push(DispatchBinding {
            authorized_identity: identity.clone(),
            dispatch_alias: actual_alias.clone(),
            mapped_host_calls: *mapped_host_calls,
            operations: operations_by_alias
                .get(actual_alias)
                .cloned()
                .unwrap_or_default(),
            call_authority_identities: call_authorities_by_alias
                .get(actual_alias)
                .map(|identities| identities.iter().cloned().collect())
                .unwrap_or_default(),
        });
    }
    Ok(bindings)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let artifact_path = PathBuf::from(take_arg(&mut args, "artifact path")?);
    let authorized_identity = take_arg(&mut args, "authorized capability identity")?;
    let declared_dispatch_alias = take_arg(&mut args, "declared VM dispatch alias")?;
    let invocation_mode = take_arg(&mut args, "invocation mode")?;

    let artifact = fs::read(&artifact_path)?;
    let admitted = admit_owned(artifact)?;
    let document = match invocation_mode.as_str() {
        "single" => run_single(
            &mut args,
            &admitted,
            &authorized_identity,
            &declared_dispatch_alias,
        )?,
        "batch" => run_batch(
            &mut args,
            &admitted,
            &authorized_identity,
            &declared_dispatch_alias,
        )?,
        _ => return Err(std::io::Error::other("invocation mode must be single or batch").into()),
    };
    println!("{}", serde_json::to_string(&document)?);
    Ok(())
}
