//! Filesystem lifecycle for disposable, gzip-compressed probe cache entries.
//! Admission and compiler identities remain owned by the probe.

use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::Path;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy)]
struct Limits {
    bytes: u64,
    entries: usize,
    age: Duration,
}

impl Limits {
    fn configured() -> Self {
        let number = |name: &str, default| {
            std::env::var(name)
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                .unwrap_or(default)
        };
        Self {
            bytes: number("MNCS_PROBE_CACHE_MAX_BYTES", 2 * 1024 * 1024 * 1024),
            entries: number("MNCS_PROBE_CACHE_MAX_ENTRIES", 128).min(usize::MAX as u64) as usize,
            age: Duration::from_secs(number("MNCS_PROBE_CACHE_MAX_AGE_SECONDS", 14 * 86400)),
        }
    }
}

fn lock(directory: &Path, exclusive: bool) -> io::Result<File> {
    fs::create_dir_all(directory)?;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join(".retention.lock"))?;
    if exclusive {
        file.lock()
    } else {
        file.lock_shared()
    }?;
    Ok(file)
}

pub fn read_lease(directory: &str) -> Option<File> {
    lock(Path::new(directory), false).ok()
}

pub fn touch(path: &str) {
    if let Ok(file) = OpenOptions::new().write(true).open(path) {
        let _ = file.set_modified(SystemTime::now());
    }
}

fn managed_name(name: &str) -> bool {
    name.len() >= 64
        && name.as_bytes()[..64]
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        && matches!(&name[64..], ".json.gz" | ".program.json.gz")
}

fn prune(directory: &Path, limits: Limits) -> io::Result<()> {
    let mut entries = Vec::new();
    let mut bytes: u64 = 0;
    for child in fs::read_dir(directory)? {
        let child = child?;
        if !child.file_type()?.is_file() {
            continue;
        }
        let name = child.file_name().to_string_lossy().into_owned();
        #[cfg(target_os = "linux")]
        if let Some(parts) = name
            .strip_suffix(".json.gz.tmp")
            .map(|value| value.split('.').collect::<Vec<_>>())
        {
            if parts.len() == 3
                && parts[0].len() == 64
                && parts[0]
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                && parts[1].parse::<u32>().is_ok()
                && parts[2].parse::<u128>().is_ok()
                && !Path::new("/proc").join(parts[1]).exists()
            {
                fs::remove_file(child.path())?;
                continue;
            }
        }
        if !managed_name(&name) {
            continue;
        }
        let metadata = child.metadata()?;
        bytes = bytes.saturating_add(metadata.len());
        entries.push((
            metadata.modified().unwrap_or(UNIX_EPOCH),
            child.path(),
            metadata.len(),
        ));
    }
    entries.sort();
    let mut remaining = entries.len();
    for (used, path, length) in entries {
        let expired = SystemTime::now().duration_since(used).unwrap_or_default() > limits.age;
        if !expired && bytes <= limits.bytes && remaining <= limits.entries {
            break;
        }
        fs::remove_file(path)?;
        bytes = bytes.saturating_sub(length);
        remaining -= 1;
    }
    Ok(())
}

fn publish_with_limits(
    directory: &Path,
    temporary: &Path,
    destination: &Path,
    limits: Limits,
) -> io::Result<bool> {
    let _lease = lock(directory, true)?;
    prune(directory, limits)?;
    if limits.entries == 0 || fs::metadata(temporary)?.len() > limits.bytes {
        fs::remove_file(temporary)?;
        return Ok(false);
    }
    fs::rename(temporary, destination)?;
    prune(directory, limits)?;
    Ok(destination.exists())
}

pub fn publish(directory: &str, temporary: &str, destination: &str) -> bool {
    publish_with_limits(
        Path::new(directory),
        Path::new(temporary),
        Path::new(destination),
        Limits::configured(),
    )
    .unwrap_or(false)
}

/// Run the same bounded upkeep without compiling a new probe artifact.
pub fn maintain(directory: &Path) -> io::Result<()> {
    let _lease = lock(directory, true)?;
    prune(directory, Limits::configured())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    fn root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "mncs-probe-retention-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&root).unwrap();
        root
    }
    fn path(root: &Path, digit: char) -> PathBuf {
        root.join(format!("{}.json.gz", digit.to_string().repeat(64)))
    }
    fn limits() -> Limits {
        Limits {
            bytes: 8,
            entries: 2,
            age: Duration::from_secs(86400),
        }
    }
    fn put(root: &Path, digit: char, payload: &[u8]) -> bool {
        let tmp = root.join(format!("{digit}.tmp"));
        fs::write(&tmp, payload).unwrap();
        publish_with_limits(root, &tmp, &path(root, digit), limits()).unwrap()
    }
    #[test]
    fn publication_evicts_oldest_by_bytes_and_preserves_unrelated_files() {
        let root = root();
        fs::write(root.join("evidence.json.gz"), b"keep").unwrap();
        assert!(put(&root, 'a', b"1234"));
        File::options()
            .write(true)
            .open(path(&root, 'a'))
            .unwrap()
            .set_modified(SystemTime::now() - Duration::from_secs(60))
            .unwrap();
        assert!(put(&root, 'b', b"5678"));
        assert!(put(&root, 'c', b"9012"));
        assert!(!path(&root, 'a').exists());
        assert!(path(&root, 'b').exists());
        assert!(root.join("evidence.json.gz").exists());
        assert!(!put(&root, 'd', b"too large to cache"));
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn reads_refresh_recency_and_expired_entries_are_removed() {
        let root = root();
        put(&root, 'a', b"1234");
        File::options()
            .write(true)
            .open(path(&root, 'a'))
            .unwrap()
            .set_modified(UNIX_EPOCH)
            .unwrap();
        touch(path(&root, 'a').to_str().unwrap());
        assert!(path(&root, 'a').metadata().unwrap().modified().unwrap() > UNIX_EPOCH);
        File::options()
            .write(true)
            .open(path(&root, 'a'))
            .unwrap()
            .set_modified(UNIX_EPOCH)
            .unwrap();
        put(&root, 'b', b"5678");
        assert!(!path(&root, 'a').exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn abandoned_compression_files_are_reaped_without_touching_live_writers() {
        let root = root();
        let abandoned = root.join(format!("{}.4294967295.1.json.gz.tmp", "a".repeat(64)));
        let live = root.join(format!("{}.{}.1.json.gz.tmp", "b".repeat(64), std::process::id()));
        fs::write(&abandoned, b"partial").unwrap();
        fs::write(&live, b"active").unwrap();
        fs::write(root.join("evidence.json.gz.tmp"), b"keep").unwrap();
        maintain(&root).unwrap();
        assert!(!abandoned.exists());
        assert!(live.exists());
        assert!(root.join("evidence.json.gz.tmp").exists());
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn eviction_waits_for_active_readers() {
        let root = root();
        put(&root, 'a', b"1234");
        let lease = read_lease(root.to_str().unwrap()).unwrap();
        let (send, receive) = std::sync::mpsc::channel();
        let target = root.clone();
        let worker = std::thread::spawn(move || {
            put(&target, 'b', b"5678");
            send.send(()).unwrap();
        });
        assert!(receive.recv_timeout(Duration::from_millis(100)).is_err());
        drop(lease);
        receive.recv_timeout(Duration::from_secs(5)).unwrap();
        worker.join().unwrap();
        fs::remove_dir_all(root).unwrap();
    }
}
