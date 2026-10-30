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
target_revision_marker="$target_dir/.mncs-language-revision"
extract_locked_source() {
    local destination="$1"
    local selected_source="${MNCS_LANGUAGE_ROOT:-}"
    # A linked worktree's parent is not the family root. Resolve the owning
    # repository through Git before looking for its local Language provider.
    # The exact pinned commit is still checked below before extracting bytes.
    if [[ -z "$selected_source" ]]; then
        local common_directory
        common_directory=$(git rev-parse --path-format=absolute --git-common-dir)
        selected_source="$(dirname "$(dirname "$common_directory")")/mncs-language"
    fi
    if [[ -n "$selected_source" && -f "$selected_source/Cargo.toml" ]]; then
        if git -C "$selected_source" cat-file -e "${revision}^{commit}" 2>/dev/null; then
            mkdir -p "$destination"
            git -C "$selected_source" archive "$revision" | tar -x -C "$destination"
            printf '%s\n' "$revision" > "$destination/revision"
            return
        fi
    fi
    curl --fail --location "https://api.github.com/repos/epi13/mncs-language/tarball/$revision" -o "$destination/stage0.tar.gz"
    tar -xzf "$destination/stage0.tar.gz" --strip-components=1 -C "$destination"
    printf '%s\n' "$revision" > "$destination/revision"
}
if [[ ! -f .bootstrap/revision ]]; then
    if [[ -e .bootstrap/Cargo.toml ]]; then
        echo 'Unmarked Stage-0 tree: remove .bootstrap and rerun to establish the pin.' >&2
        exit 1
    fi
    mkdir -p .bootstrap
    extract_locked_source .bootstrap
fi
current_revision=$(cat .bootstrap/revision)
if [[ "$current_revision" != "$revision" ]]; then
    stage_root="$PWD/.build/stage0"
    refreshed="$stage_root/$revision"
    mkdir -p "$stage_root"
    if [[ -f "$refreshed/revision" ]]; then
        if [[ $(cat "$refreshed/revision") != "$revision" || ! -f "$refreshed/Cargo.toml" ]]; then
            echo "Invalid staged Stage-0 tree for locked revision $revision." >&2
            exit 1
        fi
    else
        if [[ -e "$refreshed/Cargo.toml" ]]; then
            echo "Unmarked staged Stage-0 tree at $refreshed; refusing to replace it." >&2
            exit 1
        fi
        mkdir -p "$refreshed"
        extract_locked_source "$refreshed"
    fi
    previous="$stage_root/$current_revision"
    if [[ -e "$previous" ]]; then
        previous="$previous-$(date +%s)"
    fi
    mv .bootstrap "$previous"
    mv "$refreshed" .bootstrap
    echo "Reprovisioned Stage-0 from $current_revision to $revision."
fi
[[ $(cat .bootstrap/revision) == "$revision" ]]
[[ -f .bootstrap/Cargo.toml ]]
target_revision=""
if [[ -f "$target_revision_marker" ]]; then
    target_revision=$(cat "$target_revision_marker")
fi
if [[ -n "$target_revision" && "$target_revision" != "$revision" ]]; then
    echo "Refreshing Stage-0 build outputs for locked Language revision $revision."
    cargo clean --release --manifest-path .bootstrap/Cargo.toml --target-dir "$target_dir"
elif [[ -z "$target_revision" ]]; then
    # A missing marker can mean an interrupted first build. Keep partial
    # outputs; Cargo fingerprints validate them and avoid discarding work.
    echo "No Stage-0 build marker; preserving outputs for Cargo validation."
fi
CARGO_TARGET_DIR="$target_dir" cargo build --release --locked --manifest-path .bootstrap/Cargo.toml -p mncs-cli
CARGO_TARGET_DIR="$target_dir" cargo build --release --locked --manifest-path tools/stage0-probe/Cargo.toml
mkdir -p "$(dirname "$target_revision_marker")"
temporary_marker="$target_revision_marker.tmp.$$"
printf '%s\n' "$revision" > "$temporary_marker"
mv "$temporary_marker" "$target_revision_marker"
