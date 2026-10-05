//! Local producer build receipt. This is bootstrap provenance, not attestation.
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn collect(root: &Path, path: &Path, rows: &mut BTreeMap<String, String>) {
    if path.is_dir() {
        if matches!(
            path.file_name().and_then(|n| n.to_str()),
            Some("target" | ".git")
        ) {
            return;
        }
        let mut files: Vec<_> = std::fs::read_dir(path)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        files.sort();
        for file in files {
            collect(root, &file, rows);
        }
    } else if path.is_file() {
        let name = path
            .strip_prefix(root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        println!("cargo:rerun-if-changed={}", path.display());
        rows.insert(name, hash(&std::fs::read(path).unwrap()));
    }
}
fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap())
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let mut inputs = BTreeMap::new();
    // Exact compiled adapter and pinned dependency closure. No current self-hosting claim.
    for name in [
        "tools/stage0-probe/src",
        "tools/stage0-probe/build.rs",
        "tools/stage0-probe/Cargo.toml",
        "tools/stage0-probe/Cargo.lock",
        "tools/vm_emit.rs",
        "tools/vm-artifact-codec/src",
        "tools/vm-artifact-codec/Cargo.toml",
        "mncs-language.lock.json",
        ".bootstrap/crates",
        ".bootstrap/Cargo.toml",
        ".bootstrap/Cargo.lock",
        ".bootstrap/revision",
        ".bootstrap/library/stdlib-bundle.json",
    ] {
        collect(&root, &root.join(name), &mut inputs);
    }
    // Cargo must refresh the observation after a commit/worktree HEAD change.
    for arguments in [
        vec!["rev-parse", "--git-path", "HEAD"],
        vec!["symbolic-ref", "-q", "HEAD"],
    ] {
        if let Ok(output) = std::process::Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(&arguments)
            .output()
        {
            if output.status.success() {
                let name = String::from_utf8_lossy(&output.stdout).trim().to_owned();
                let path = if arguments[0] == "symbolic-ref" {
                    std::process::Command::new("git")
                        .arg("-C")
                        .arg(&root)
                        .args(["rev-parse", "--git-path", &name])
                        .output()
                        .ok()
                        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
                        .unwrap_or(name)
                } else {
                    name
                };
                println!("cargo:rerun-if-changed={}", root.join(path).display());
            }
        }
    }
    println!("cargo:rerun-if-env-changed=RUSTFLAGS");
    let revision = std::process::Command::new("git")
        .args(["-C", root.to_str().unwrap(), "rev-parse", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned());
    let stage0_revision = std::fs::read_to_string(root.join(".bootstrap/revision"))
        .unwrap()
        .trim()
        .to_owned();
    let compiler = std::process::Command::new(std::env::var("RUSTC").unwrap())
        .arg("-vV")
        .output()
        .unwrap();
    let receipt = serde_json::json!({"schema_version":"mncs.compiler-producer-build/1", "repository":"mncs-compiler", "producer_kind":"stage0-bootstrap-direct-emitter", "source_revision":revision, "source_inputs":inputs, "stage0_revision":stage0_revision, "rustc":String::from_utf8_lossy(&compiler.stdout).trim(), "build_configuration":{"profile":std::env::var("PROFILE").ok(),"target":std::env::var("TARGET").ok(),"opt_level":std::env::var("OPT_LEVEL").ok(),"rustflags":std::env::var("RUSTFLAGS").ok()}, "assurance":"local build observation; not independent attestation"});
    let bytes = serde_json::to_vec(&receipt).unwrap();
    let value =
        serde_json::json!({"identity":format!("sha256:{}", hash(&bytes)), "receipt":receipt});
    std::fs::write(
        PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("producer.json"),
        serde_json::to_vec(&value).unwrap(),
    )
    .unwrap();
}
