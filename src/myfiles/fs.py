"""Filesystem helpers."""

import os

from myfiles.paths import is_within


def same_content(a: str, b: str) -> bool:
    """Return ``True`` if the files at ``a`` and ``b`` have identical contents."""
    try:
        with open(a, "rb") as file_a, open(b, "rb") as file_b:
            while True:
                chunk_a = file_a.read(65536)
                chunk_b = file_b.read(65536)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def iter_tracked_files(base_dir: str) -> list[str]:
    """Return sorted relative paths of the regular files tracked under ``base_dir``.

    The ``.git`` directory and symbolic links are ignored.
    """
    rels: list[str] = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, base_dir)
            rels.append(rel)
    return sorted(rels)


def prune_empty_dirs(start: str, base_dir: str) -> None:
    """Remove empty parent directories from ``start`` up to (excluding) ``base_dir``."""
    current = start
    while current and is_within(base_dir, current) and current != base_dir:
        try:
            if os.listdir(current):
                break
            os.rmdir(current)
        except OSError:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
