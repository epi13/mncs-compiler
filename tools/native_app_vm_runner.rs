use std::{collections::BTreeSet, env, fs, path::PathBuf};

use mncs_model::{ExecutionRequest, ExecutionValue};
use mncs_vm::{
    admit::admit_owned,
    capability::{CapabilityEnv, EffectRequest, EffectResponse, Provider},
    outcome::Outcome,
    session::{CallSpec, Session},
    value::{from_wire, to_wire, Value},
};

const PROVIDER_ID: &str = "mncs.compiler.test-structured-file-provider/1";
const INPUT_SCHEMA: &str = "mncs.test-digest-request/1";
const OUTPUT_SCHEMA: &str = "mncs.test-digest/1";

struct StructuredFileProvider {
    capability: String,
    input_path: PathBuf,
    output_path: PathBuf,
    fail_write: bool,
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
                if schema != INPUT_SCHEMA {
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

fn take_arg(args: &mut impl Iterator<Item = String>, label: &str) -> Result<String, String> {
    args.next().ok_or_else(|| format!("missing {label}"))
}

fn execute_call(
    session: &mut Session<'_>,
    request_path: &PathBuf,
    input_path: &PathBuf,
    output_path: &PathBuf,
    authorized_identity: &str,
    dispatch_alias: &str,
    mapped_host_calls: usize,
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
    let caps = if mode == "none" {
        CapabilityEnv::empty()
    } else {
        CapabilityEnv::empty().bind(
            dispatch_alias,
            StructuredFileProvider {
                capability: dispatch_alias.to_owned(),
                input_path: input_path.clone(),
                output_path: output_path.clone(),
                fail_write: mode == "fail-write",
            },
        )
    };
    let (outcome, record) = session.call(&caps, spec);
    let returned_wire = record.returned.iter().map(to_wire).collect::<Vec<_>>();
    Ok(serde_json::json!({
        "outcome": outcome,
        "record": record,
        "returned_wire": returned_wire,
        "capability_binding": {
            "authorized_capability_identity": authorized_identity,
            "runtime_dispatch_alias": dispatch_alias,
            "contract_dispatch_alias": dispatch_alias,
            "mapped_host_call_count": mapped_host_calls,
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
    let (dispatch_alias, mapped_host_calls) =
        capability_dispatch_binding(admitted, authorized_identity)
            .map_err(std::io::Error::other)?;
    if dispatch_alias != declared_dispatch_alias {
        return Err(std::io::Error::other(
            "application contract dispatch alias differs from admitted SSA",
        )
        .into());
    }
    let mut session = Session::open(admitted);
    execute_call(
        &mut session,
        &request_path,
        &input_path,
        &output_path,
        authorized_identity,
        &dispatch_alias,
        mapped_host_calls,
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

    let (dispatch_alias, mapped_host_calls) =
        capability_dispatch_binding(admitted, authorized_identity)
            .map_err(std::io::Error::other)?;
    if dispatch_alias != declared_dispatch_alias {
        return Err(std::io::Error::other(
            "application contract dispatch alias differs from admitted SSA",
        )
        .into());
    }

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
            authorized_identity,
            &dispatch_alias,
            mapped_host_calls,
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

fn capability_dispatch_binding(
    admitted: &mncs_vm::admit::Admitted,
    authorized_identity: &str,
) -> Result<(String, usize), String> {
    if !admitted
        .artifact
        .requirements
        .capabilities
        .iter()
        .any(|required| required == authorized_identity)
    {
        return Err(format!(
            "artifact does not require authorized capability {authorized_identity}"
        ));
    }
    let ssa = admitted
        .ssa_module()
        .ok_or_else(|| "admitted artifact does not contain selected SSA".to_owned())?;
    let mut aliases = std::collections::BTreeMap::<String, usize>::new();
    let mut host_call_count = 0usize;
    for instruction in ssa
        .functions
        .iter()
        .flat_map(|function| &function.blocks)
        .flat_map(|block| &block.instructions)
    {
        if let mncs_model::SsaInstructionKind::HostCall { capability, .. } = &instruction.kind {
            host_call_count += 1;
            let matches = instruction
                .capability_uses
                .iter()
                .filter(|usage| usage.capability.0 == authorized_identity)
                .count();
            if matches != 1 {
                return Err(format!(
                    "HostCall {capability:?} has {matches} uses of authorized identity {authorized_identity}"
                ));
            }
            *aliases.entry(capability.clone()).or_default() += 1;
        }
    }
    if host_call_count == 0 || aliases.len() != 1 {
        return Err(format!(
            "expected HostCalls to resolve one runtime alias, found {host_call_count} HostCalls and {} aliases",
            aliases.len()
        ));
    }
    let (alias, mapped_calls) = aliases.into_iter().next().expect("one alias checked");
    if mapped_calls != host_call_count {
        return Err("not every HostCall maps to the same authorized capability".to_owned());
    }
    Ok((alias, host_call_count))
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
