"""Path mapping and normalization helpers."""

import os

#: Name of the standard base-dir subdirectory inside the dotfiles repo root.
BASE_DIR_NAME = "files"


def repo_base_dir(repo_root: str) -> str:
    """Return the base-dir (where tracked files live) inside ``repo_root``."""
    return os.path.join(repo_root, BASE_DIR_NAME)


def absolute(path: str | os.PathLike[str]) -> str:
    """Return an absolute, normalized path with ``~`` expanded."""
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def target_to_relative(target: str, root: str = "/") -> str:
    """Map an absolute target path (under ``root``) to its relative path in the base directory.

    The leading ``root`` is stripped: ``/etc/fstab`` -> ``etc/fstab``.
    """
    return os.path.relpath(target, root)


def relative_to_target(rel: str, root: str = "/") -> str:
    """Map a relative base-directory path back to an absolute target path under ``root``."""
    return os.path.join(root, rel)


def is_within(directory: str, path: str) -> bool:
    """Return ``True`` if ``path`` is equal to or inside ``directory``."""
    directory = os.path.normpath(directory)
    path = os.path.normpath(path)
    try:
        common = os.path.commonpath([directory, path])
    except ValueError:
        return False
    return common == directory


def display_path(path: str) -> str:
    """Return a user-friendly version of ``path``, using ``~`` for the home directory."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path
