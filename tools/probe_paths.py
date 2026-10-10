"""Shared paths for compiler probe caches and their provisioned Stage-0 toolchain."""

from pathlib import Path
import subprocess


def _git_common_worktree_root(repository_root: Path) -> Path | None:
    """Return a normal repository's shared checkout root when Git can prove it."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    common_directory = result.stdout.strip()
    if not common_directory:
        return None
    common_path = Path(common_directory).expanduser()
    if not common_path.is_absolute():
        common_path = repository_root / common_path
    common_path = common_path.resolve()
    # A normal non-bare repository and its linked worktrees share the main
    # checkout's .git directory. Unknown/bare layouts stay checkout-local.
    return common_path.parent if common_path.name == ".git" else None


def probe_cache_directory(repository_root: Path) -> Path:
    """Use the shared checkout's ignored cache when linked worktrees prove it."""
    root = Path(repository_root).resolve()
    cache_root = _git_common_worktree_root(root) or root
    return cache_root / ".build" / "probe-cache"


def probe_toolchain_identity_root(repository_root: Path) -> Path:
    """Prefer a local bootstrap, else reuse the linked checkout's pinned one."""
    root = Path(repository_root).resolve()
    if (root / ".bootstrap" / "revision").is_file():
        return root
    common_root = _git_common_worktree_root(root)
    if common_root and (common_root / ".bootstrap" / "revision").is_file():
        return common_root
    return root
