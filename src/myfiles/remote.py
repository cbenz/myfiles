"""SSH helpers for the ``remotes/`` feature (synchronizing remote hosts).

Each subdirectory of ``remotes/`` (a sibling of the ``files/`` base directory)
is a host: its contents mirror the remote filesystem, the leading ``/`` of a
remote path being stripped (``ender3:/home/admin/x`` -> ``remotes/ender3/home/admin/x``).
All host connection details are delegated to the user's SSH configuration —
``ssh <host>`` must work as configured. There are **no symlinks on a remote
host**: files are copied in one direction or the other, only when their content
differs (compared by SHA-256 hash).
"""

import os
import shlex
import subprocess

REMOTES_DIR_NAME = "remotes"


class RemoteError(Exception):
    """Raised when an SSH operation fails (unreachable host, missing file...)."""


def remotes_dir_for_base(base_dir: str) -> str:
    """Return the ``remotes/`` directory of the repository (sibling of ``files/``)."""
    return os.path.join(os.path.dirname(base_dir), REMOTES_DIR_NAME)


def host_dir(base_dir: str, host: str) -> str:
    """Return the tracked directory for ``host`` (``<repo>/remotes/<host>``)."""
    return os.path.join(remotes_dir_for_base(base_dir), host)


def parse_remote_arg(arg: str) -> tuple[str, str] | None:
    """Parse the ``host:/abs/path`` syntax.

    Returns ``(host, absolute_remote_path)``, or ``None`` when ``arg`` is not a
    remote path (e.g. a plain local path).
    """
    host, sep, path = arg.partition(":")
    if not sep or not host or "/" in host or not path.startswith("/"):
        return None
    return host, path


def remote_rel(remote_path: str) -> str:
    """Map a remote absolute path to its relative path under ``remotes/<host>``."""
    return remote_path.lstrip("/")


def format_remote(host: str, remote_path: str) -> str:
    """Return the ``host:/path`` form of a remote path."""
    return f"{host}:{remote_path}"


def _ssh(host: str, command: str) -> subprocess.CompletedProcess[str]:
    """Run ``command`` on ``host`` (through the user's SSH config)."""
    return subprocess.run(
        ["ssh", host, command],
        check=False,
        capture_output=True,
        text=True,
    )


def remote_lexists(host: str, path: str) -> bool:
    """Return ``True`` if ``path`` exists on the remote host."""
    return _ssh(host, f"test -e {shlex.quote(path)}").returncode == 0


def remote_isdir(host: str, path: str) -> bool:
    """Return ``True`` if ``path`` is a directory on the remote host."""
    return _ssh(host, f"test -d {shlex.quote(path)}").returncode == 0


def remote_hash(host: str, path: str) -> str | None:
    """Return the SHA-256 of the remote file at ``path`` (``None`` if missing)."""
    result = _ssh(host, f"sha256sum {shlex.quote(path)}")
    if result.returncode != 0:
        return None
    return result.stdout.split(maxsplit=1)[0] if result.stdout else None


def remote_mtime(host: str, path: str) -> float | None:
    """Return the mtime (epoch seconds) of the remote file (``None`` if missing)."""
    result = _ssh(host, f"stat -c %Y {shlex.quote(path)}")
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def remote_mode(host: str, path: str) -> int | None:
    """Return the permission bits of the remote file (``None`` if unavailable).

    Only the classic nine permission bits are returned: the setuid/setgid/sticky
    bits are dropped (they cannot be stored by git, and copying them around is
    out of scope). ``None`` is returned when the file is missing or the host
    does not answer ``stat -c %a`` (non-GNU ``stat``), in which case permissions
    are left untouched.
    """
    result = _ssh(host, f"stat -c %a {shlex.quote(path)}")
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip(), 8) & 0o777
    except ValueError:
        return None


def remote_walk(host: str, path: str) -> list[str]:
    """Return the absolute remote paths of the regular files under ``path``."""
    result = _ssh(host, f"find {shlex.quote(path)} -type f")
    if result.returncode != 0:
        raise RemoteError(
            f"cannot list remote directory {format_remote(host, path)}: "
            f"{result.stderr.strip()}"
        )
    return sorted(line for line in result.stdout.splitlines() if line)


def remote_mkdirs(host: str, path: str) -> None:
    """Create ``path`` (and its parents) on the remote host."""
    result = _ssh(host, f"mkdir -p {shlex.quote(path)}")
    if result.returncode != 0:
        raise RemoteError(
            f"cannot create remote directory {format_remote(host, path)}: "
            f"{result.stderr.strip()}"
        )


def remote_download(
    host: str, remote_path: str, local_path: str, mode: int | None = None
) -> None:
    """Download a remote file to ``local_path`` (binary-safe).

    When ``mode`` is given, ``local_path`` receives those permission bits.
    """
    quoted = shlex.quote(remote_path)
    with open(local_path, "wb") as f:
        result = subprocess.run(
            ["ssh", host, f"cat {quoted}"],
            check=False,
            stdout=f,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0:
        raise RemoteError(
            f"cannot download {format_remote(host, remote_path)}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    if mode is not None:
        os.chmod(local_path, mode & 0o777)


def remote_upload(
    local_path: str, host: str, remote_path: str, mode: int | None = None
) -> None:
    """Upload ``local_path`` to a remote file (binary-safe).

    When ``mode`` is given, the remote file receives those permission bits: the
    ``chmod`` is chained after the copy, so a single SSH command (and a single
    round-trip) is used, the content still flowing through stdin.
    """
    quoted = shlex.quote(remote_path)
    command = f"cat > {quoted}"
    if mode is not None:
        command += f" && chmod {mode & 0o777:o} {quoted}"
    with open(local_path, "rb") as f:
        result = subprocess.run(
            ["ssh", host, command],
            check=False,
            stdin=f,
            capture_output=True,
        )
    if result.returncode != 0:
        raise RemoteError(
            f"cannot upload {format_remote(host, remote_path)}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def iter_remote_files(base_dir: str) -> list[tuple[str, str]]:
    """Return the sorted ``(host, rel)`` pairs of the files tracked under ``remotes/``."""
    root = remotes_dir_for_base(base_dir)
    if not os.path.isdir(root):
        return []
    result: list[tuple[str, str]] = []
    for host in sorted(os.listdir(root)):
        host_path = os.path.join(root, host)
        if not os.path.isdir(host_path):
            continue
        for dirpath, dirnames, filenames in os.walk(host_path):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, host_path)
                result.append((host, rel))
    return sorted(result)
