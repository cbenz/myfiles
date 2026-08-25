"""Optional Git integration."""

import subprocess


def is_git_repo(base_dir: str) -> bool:
    """Return ``True`` if ``base_dir`` is inside a Git work tree."""
    try:
        result = subprocess.run(
            ["git", "-C", base_dir, "rev-parse", "--is-inside-work-tree"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError, OSError:
        return False
    return result.returncode == 0


def is_dirty(base_dir: str, rel: str) -> bool:
    """Return ``True`` if the file at ``rel`` has uncommitted changes.

    Covers modified, staged and untracked states. Returns ``False`` when
    ``base_dir`` is not a Git repository.
    """
    if not is_git_repo(base_dir):
        return False
    result = subprocess.run(
        ["git", "-C", base_dir, "status", "--porcelain", "--", rel],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return bool(result.stdout.strip())
