#!/usr/bin/env bash
# All bootstrap writes are confined to this repository's ignored directories.
set -euo pipefail
cd "$(dirname "$0")/.."
# Keep the reference build useful in constrained compiler-workload sandboxes.
# Both artifacts are disposable bootstrap outputs; callers may override these
# defaults when they want full symbols or incremental compilation.
export CARGO_PROFILE_DEV_DEBUG="${CARGO_PROFILE_DEV_DEBUG:-0}"
export CARGO_INCREMENTAL="${CARGO_INCREMENTAL:-0}"
revision=$(python3 -c 'import json; print(json.load(open("mncs-language.lock.json"))["revision"])')
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
cargo build --locked --manifest-path .bootstrap/Cargo.toml -p mncs-cli
CARGO_TARGET_DIR="$PWD/.bootstrap/target" cargo build --locked --manifest-path tools/stage0-probe/Cargo.toml
