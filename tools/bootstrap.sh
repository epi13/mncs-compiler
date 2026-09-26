#!/usr/bin/env bash
# The pinned Stage-0 source stays under .bootstrap. Build outputs can be moved
# to a caller-selected target directory when the repository filesystem is
# quota constrained; the default remains local and disposable.
set -euo pipefail
cd "$(dirname "$0")/.."
# Keep the reference build useful in constrained compiler-workload sandboxes.
# Both artifacts are disposable bootstrap outputs; callers may override these
# defaults when they want full symbols or incremental compilation.
export CARGO_PROFILE_RELEASE_DEBUG="${CARGO_PROFILE_RELEASE_DEBUG:-0}"
export CARGO_INCREMENTAL="${CARGO_INCREMENTAL:-0}"
revision=$(python3 -c 'import json; print(json.load(open("mncs-language.lock.json"))["revision"])')
target_dir="${MNCS_BOOTSTRAP_TARGET_DIR:-$PWD/.bootstrap/target}"
if [[ ! -f .bootstrap/revision ]]; then
    if [[ -e .bootstrap/Cargo.toml ]]; then
        echo 'Unmarked Stage-0 tree: remove .bootstrap and rerun to establish the pin.' >&2
        exit 1
    fi
    mkdir -p .bootstrap
    curl --fail --location "https://api.github.com/repos/epi13/mncs-language/tarball/$revision" -o .bootstrap/stage0.tar.gz
    tar -xzf .bootstrap/stage0.tar.gz --strip-components=1 -C .bootstrap
    printf '%s\n' "$revision" > .bootstrap/revision
fi
[[ $(cat .bootstrap/revision) == "$revision" ]]
CARGO_TARGET_DIR="$target_dir" cargo build --release --locked --manifest-path .bootstrap/Cargo.toml -p mncs-cli
CARGO_TARGET_DIR="$target_dir" cargo build --release --locked --manifest-path tools/stage0-probe/Cargo.toml
