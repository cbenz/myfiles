"""Implementation of the myfiles commands."""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import pathspec

from myfiles import remote
from myfiles.fs import hash_file, iter_tracked_files, prune_empty_dirs, same_content
from myfiles.git import is_dirty
from myfiles.paths import (
    BASE_DIR_NAME,
    absolute,
    display_path,
    is_within,
    relative_to_target,
    repo_base_dir,
    target_to_relative,
)


@dataclass
class Action:
    """A planned filesystem change, with a human-readable description."""

    description: str
    apply: Callable[[], None]


def _ask_confirmation(
    prompt: str = "Apply these changes? [Y/n] ", abort_on_interrupt: bool = False
) -> bool:
    """Ask the user to confirm; empty input or ``y``/``yes`` means yes.

    With ``abort_on_interrupt``, Ctrl-C/EOF re-raise instead of returning no
    (used by the per-item ``fix`` confirmation).
    """
    try:
        answer = input(prompt).strip().lower()
    except EOFError, KeyboardInterrupt:
        if abort_on_interrupt:
            raise
        return False
    return answer in ("", "y", "yes")


def _announce_dry_run(dry_run: bool) -> None:
    """Print a first line when running in dry-run mode."""
    if dry_run:
        print("dry-run: preview only, nothing will be changed")


def _execute(
    actions: list[Action],
    infos: list[str],
    errors: list[str],
    dry_run: bool,
    abort_on_interrupt: bool = False,
    auto_confirm: bool = False,
) -> int:
    """Print the plan, then apply it after confirmation (unless ``--dry-run``).

    Return ``1`` on error, ``2`` when the user cancelled the confirmation, and
    ``0`` otherwise (applied, dry-run, or nothing to do). With
    ``abort_on_interrupt``, Ctrl-C/EOF during the confirmation raises
    ``_FixAborted`` (used by the per-item ``fix``) instead of cancelling; with
    ``auto_confirm`` the plan is applied without asking (``fix --defaults``).
    """
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    for action in actions:
        print(action.description)
    for info in infos:
        print(info)
    if not actions:
        return 0
    if dry_run:
        return 0
    if auto_confirm:
        confirmed = True
    else:
        try:
            confirmed = _ask_confirmation(abort_on_interrupt=abort_on_interrupt)
        except EOFError, KeyboardInterrupt:
            raise _FixAborted
    if not confirmed:
        print("cancelled")
        return 2
    for action in actions:
        action.apply()
    print(f"{len(actions)} change(s) applied")
    return 0


def resolve_base_dir(base_dir: str | None) -> str | None:
    """Return the effective base directory (``<repo root>/files``).

    Precedence: the ``--base-dir`` argument (the dotfiles repository root),
    then the ``MYFILES_BASE_DIR`` environment variable, then the ``files/``
    base directory found by walking up from the current directory. Prints an
    error (and returns ``None``) when no repository root is available.
    """
    if base_dir:
        root = absolute(base_dir)
    else:
        env = os.environ.get("MYFILES_BASE_DIR")
        if env:
            root = absolute(env)
        else:
            root = _find_repo_root()
            if root is None:
                print(
                    "error: no base directory set; run `myfiles` from the dotfiles "
                    "repository, pass --base-dir, or set MYFILES_BASE_DIR"
                )
                return None
    return repo_base_dir(root)


def _find_repo_root() -> str | None:
    """Return the first ancestor of the CWD that is a myfiles repository root.

    A repository root is recognized by the presence of the standard ``files``
    base directory (the tracked files live in ``<root>/files``).
    """
    current = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(current, BASE_DIR_NAME)):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def capture(
    base_dir: str | None,
    paths: list[str],
    ignore: list[str],
    force: bool,
    dry_run: bool,
    root: str = "/",
    remotes: list[str] | None = None,
) -> int:
    """Move files into ``base_dir``, then deploy symlinks for them.

    ``host:/path`` arguments capture remote files/directories (copied from the
    host into ``remotes/<host>``, no symlinks). ``--remotes`` (optional host
    names, or all hosts when no value) recaptures every known remote file whose
    content differs from the host. The raw ``--remotes`` values may include
    positional-looking tokens swallowed by argparse's ``nargs="*"``: path-like
    ones are moved back to ``paths``.
    """
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    if remotes is not None:
        hosts, stray = _split_remote_hosts(remotes)
        paths = stray + paths
        remotes = hosts

    remote_paths = [
        parsed for arg in paths if (parsed := remote.parse_remote_arg(arg)) is not None
    ]
    local_paths = [arg for arg in paths if remote.parse_remote_arg(arg) is None]

    rc = 0
    if remote_paths:
        rc = _capture_remote_paths(base_dir, remote_paths, dry_run)
    if local_paths:
        rc = max(
            rc, _capture_local(base_dir, local_paths, ignore, force, dry_run, root)
        )
    if remotes is not None:
        rc = max(rc, _capture_remote_scan(base_dir, remotes, dry_run))
    return rc


def _split_remote_hosts(remotes: list[str]) -> tuple[list[str], list[str]]:
    """Split raw ``--remotes`` values into ``(hosts, stray_paths)``.

    Argparse's ``nargs="*"`` greedily swallows the positional-looking tokens
    that follow ``--remotes``; path-like ones (a leading ``/``, any ``/``, or a
    ``host:/...`` form) are moved back to the positional paths.
    """
    hosts: list[str] = []
    stray: list[str] = []
    for value in remotes:
        if (
            value.startswith("/")
            or "/" in value
            or remote.parse_remote_arg(value) is not None
        ):
            stray.append(value)
        else:
            hosts.append(value)
    return hosts, stray


def _capture_local(
    base_dir: str,
    paths: list[str],
    ignore: list[str],
    force: bool,
    dry_run: bool,
    root: str,
) -> int:
    """Local capture: move files into ``base_dir``, then deploy symlinks for them."""
    plan: list[tuple[str, str, bool]] = []  # (real_src, rel, already_present)
    errors: list[str] = []
    infos: list[str] = []
    extra_actions: list[Action] = []

    for arg in paths:
        _plan_capture(
            base_dir,
            arg,
            ignore,
            force,
            plan,
            infos,
            errors,
            root,
            extra_actions=extra_actions,
        )

    return _capture_tail(
        base_dir,
        plan,
        infos,
        errors,
        dry_run,
        root,
        extra_actions=extra_actions,
    )


def _capture_tail(
    base_dir: str,
    plan: list[tuple[str, str, bool]],
    infos: list[str],
    errors: list[str],
    dry_run: bool,
    root: str,
    extra_actions: list[Action] | None = None,
) -> int:
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1

    actions = list(extra_actions) if extra_actions else []
    if not plan and not actions:
        for info in infos:
            print(info)
        if not infos:
            print("nothing to capture")
        return 0

    # Deduplicate overlapping captures (e.g. the same directory passed twice).
    seen: set[str] = set()
    for real_src, rel, already_present in plan:
        if rel in seen:
            continue
        seen.add(rel)
        dest = os.path.join(base_dir, rel)
        target = relative_to_target(rel, root)
        if already_present:
            # The file is already tracked with identical content: only the
            # symlink is (re)created. The `link ... (replace: identical
            # content)` action below already says everything, so no extra
            # `skip` line is printed.
            actions.append(
                _link_action(target, base_dir, rel, reason="identical content")
            )
            continue
        actions.append(
            Action(
                f"move-and-link {display_path(target)} -> {display_path(base_dir)}/{rel}",
                lambda s=real_src, d=dest, t=target, b=base_dir, r=rel: (
                    _apply_move_and_link(s, d, t, b, r)
                ),
            )
        )

    rc = _execute(actions, infos, errors, dry_run)
    if rc == 1:
        return 1
    if rc == 2:
        return 0  # cancelled: nothing applied
    return 0


def _plan_capture_tail(
    base_dir: str,
    plan: list[tuple[str, str, bool]],
    infos: list[str],
    errors: list[str],
    root: str,
    extra_actions: list[Action] | None = None,
) -> list[Action]:
    """Turn a capture plan into actions (no execution).

    Same action-building logic as ``_capture_tail``: deduplicates overlapping
    rels and produces ``link ... (replace: identical content)`` or
    ``move-and-link ...`` actions.
    """
    actions = list(extra_actions) if extra_actions else []
    seen: set[str] = set()
    for real_src, rel, already_present in plan:
        if rel in seen:
            continue
        seen.add(rel)
        dest = os.path.join(base_dir, rel)
        target = relative_to_target(rel, root)
        if already_present:
            actions.append(
                _link_action(target, base_dir, rel, reason="identical content")
            )
            continue
        actions.append(
            Action(
                f"move-and-link {display_path(target)} -> {display_path(base_dir)}/{rel}",
                lambda s=real_src, d=dest, t=target, b=base_dir, r=rel: (
                    _apply_move_and_link(s, d, t, b, r)
                ),
            )
        )
    return actions


def deploy(
    base_dir: str | None,
    paths: list[str],
    force: bool,
    dry_run: bool,
    root: str = "/",
    remotes: list[str] | None = None,
) -> int:
    """Create symlinks for the tracked files in ``base_dir``.

    ``host:/path`` and ``remotes/...`` paths deploy the tracked remote files to
    the host instead (copied, only when they differ — no symlinks on a remote).
    ``--remotes`` (optional host names, or all hosts when no value) deploys
    every known remote file of those hosts to the host.
    """
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    if remotes is not None:
        hosts, stray = _split_remote_hosts(remotes)
        paths = stray + paths
        remotes = hosts
    rc = 0
    if remotes is not None:
        rc = _deploy_remote_scan(base_dir, remotes, dry_run)
    if paths:
        remote_sel, local_paths = _split_remote_selection(base_dir, paths)
        if remote_sel:
            rc = max(rc, _deploy_remote(base_dir, remote_sel, dry_run))
        if local_paths:
            rc = max(rc, _deploy_local(base_dir, local_paths, force, dry_run, root))
    elif remotes is None:
        # No PATH and no --remotes: deploy everything locally (existing
        # behavior, e.g. `myfiles deploy` invoked internally without paths).
        rc = max(rc, _deploy_local(base_dir, [], force, dry_run, root))
    return rc


def _deploy_remote_scan(base_dir: str, hosts: list[str], dry_run: bool) -> int:
    """Deploy every known remote file of the given hosts (or all hosts) to the host.

    Each tracked file under ``remotes/<host>`` is copied to the host only when
    its content differs (missing remote parents are created). An unknown host
    is an error.
    """
    errors = _validate_remote_hosts(base_dir, hosts)
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    known = remote.iter_remote_files(base_dir)
    if hosts:
        wanted = set(hosts)
        known = [(h, r) for h, r in known if h in wanted]
    if not known:
        print("nothing to deploy")
        return 0
    return _deploy_remote(base_dir, known, dry_run)


def _deploy_local(
    base_dir: str,
    paths: list[str],
    force: bool,
    dry_run: bool,
    root: str,
) -> int:
    """Local deploy: create the symlinks for the tracked files in ``base_dir``."""
    rels = _select_rels(base_dir, paths)
    if not rels:
        print("nothing to deploy")
        return 0

    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    for rel in rels:
        tracked = os.path.join(base_dir, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            if paths:
                # An explicitly requested path that has no tracked file.
                infos.append(
                    f"skip {display_path(relative_to_target(rel, root))}: not a tracked file"
                )
            continue
        _plan_deploy_one(
            relative_to_target(rel, root), rel, base_dir, force, actions, infos, errors
        )
    rc = _execute(actions, infos, errors, dry_run)
    return 0 if rc == 2 else rc


def _split_remote_selection(
    base_dir: str, paths: list[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split ``paths`` into remote ``(host, rel)`` selections and local paths.

    A path is remote when it uses the ``host:/abs/path`` syntax, is inside the
    ``remotes/`` directory, or is a root-relative ``remotes/...`` path.
    """
    remote_sel: list[tuple[str, str]] = []
    local_paths: list[str] = []
    for p in paths:
        parsed = remote.parse_remote_arg(p)
        if parsed is not None:
            host, remote_path = parsed
            remote_sel.append((host, remote.remote_rel(remote_path)))
            continue
        resolved = absolute(p)
        if is_within(remote.remotes_dir_for_base(base_dir), resolved):
            host, _, sub = os.path.relpath(
                resolved, remote.remotes_dir_for_base(base_dir)
            ).partition("/")
            remote_sel.append((host, sub))
            continue
        norm = os.path.normpath(os.path.expanduser(p).lstrip("/"))
        if norm.startswith(remote.REMOTES_DIR_NAME + "/"):
            host, _, sub = (
                norm[len(remote.REMOTES_DIR_NAME) :].lstrip("/").partition("/")
            )
            remote_sel.append((host, sub))
            continue
        local_paths.append(p)
    return remote_sel, local_paths


def _repo_gitignore_spec(base_dir: str) -> pathspec.PathSpec[Any] | None:
    """Return the gitignore spec for the repository root's ``.gitignore`` (or ``None``)."""
    repo_root = os.path.dirname(base_dir)
    gitignore = os.path.join(repo_root, ".gitignore")
    if not os.path.isfile(gitignore):
        return None
    with open(gitignore, encoding="utf-8") as f:
        lines = [
            line for line in f.read().splitlines() if line and not line.startswith("#")
        ]
    if not lines:
        return None
    return cast(
        pathspec.PathSpec[Any], pathspec.PathSpec.from_lines("gitignore", lines)
    )


def ls(base_dir: str | None, root: str = "/", remotes: list[str] | None = None) -> int:
    """List the tracked files as target paths (leading ``/``), one per line.

    Files matching the repository's ``.gitignore`` are not shown: only the
    files git actually versions are listed. With ``--remotes`` (optional host
    names, or every host when no value), the tracked remote files are listed
    instead, as ``host:/path``.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    if remotes is not None:
        hosts, _stray = _split_remote_hosts(remotes)
        return _ls_remote(base_dir, hosts)
    spec = _repo_gitignore_spec(base_dir)
    for rel in iter_tracked_files(base_dir):
        if spec is not None and spec.match_file(os.path.join(BASE_DIR_NAME, rel)):
            continue  # gitignored: not versioned
        print(relative_to_target(rel, root))
    return 0


def _ls_remote(base_dir: str, hosts: list[str]) -> int:
    """List the tracked remote files of ``hosts`` (all hosts when empty) as ``host:/path``."""
    errors = _validate_remote_hosts(base_dir, hosts)
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    spec = _repo_gitignore_spec(base_dir)
    files = remote.iter_remote_files(base_dir)
    if hosts:
        wanted = set(hosts)
        files = [(h, r) for h, r in files if h in wanted]
    for host, rel in files:
        if spec is not None and spec.match_file(
            os.path.join(remote.REMOTES_DIR_NAME, host, rel)
        ):
            continue  # gitignored: not versioned
        print(remote.format_remote(host, "/" + rel))
    return 0


def _validate_remote_hosts(base_dir: str, hosts: list[str]) -> list[str]:
    """Return error messages for the hosts that have no ``remotes/<host>`` directory."""
    return [
        f"{host}: not a known remote (no {remote.REMOTES_DIR_NAME}/{host} directory)"
        for host in hosts
        if not os.path.isdir(remote.host_dir(base_dir, host))
    ]


GITIGNORE_HEADER = "# managed by myfiles — add entries with `myfiles ignore <path>`"


def ignore(
    base_dir: str | None, paths: list[str], dry_run: bool, root: str = "/"
) -> int:
    """Add paths to the repository's ``.gitignore`` (resolved under ``files/``).

    Each path is mapped to its tracked location inside the base directory
    (e.g. ``~/.config/zsh/.antidote`` -> ``files/home/user/.config/zsh/.antidote``)
    and appended to ``.gitignore`` at the repository root (created if absent).
    Existing entries are left untouched.
    """
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    entries = sorted(
        {os.path.join(BASE_DIR_NAME, _path_to_rel(base_dir, p)) for p in paths}
    )
    action = _gitignore_action(base_dir, entries)
    if action is None:
        print("nothing to ignore (already in .gitignore)")
        return 0
    rc = _execute([action], [], [], dry_run)
    return 0 if rc == 2 else rc


def _gitignore_action(base_dir: str, entries: list[str]) -> Action | None:
    """Return an Action appending ``files/<rel>`` entries to the repo's ``.gitignore``.

    Returns ``None`` when every entry is already present (nothing to add).
    """
    repo_root = os.path.dirname(base_dir)
    gitignore = os.path.join(repo_root, ".gitignore")
    existing: set[str] = set()
    if os.path.isfile(gitignore):
        with open(gitignore, encoding="utf-8") as f:
            existing = {
                line
                for line in f.read().splitlines()
                if line and not line.startswith("#")
            }
    new_entries = sorted({e for e in entries if e not in existing})
    if not new_entries:
        return None
    if os.path.isfile(gitignore):
        return Action(
            f"append to {display_path(gitignore)}: {' '.join(new_entries)}",
            lambda p=gitignore, es=new_entries: _append_gitignore(p, es),
        )
    return Action(
        f"write {display_path(gitignore)}: {' '.join(new_entries)}",
        lambda p=gitignore, es=new_entries: _write_gitignore(p, es),
    )


def _write_gitignore(path: str, entries: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(GITIGNORE_HEADER + "\n")
        f.writelines(entry + "\n" for entry in entries)


def _append_gitignore(path: str, entries: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.writelines(entry + "\n" for entry in entries)


def eject(
    base_dir: str | None, paths: list[str], dry_run: bool, root: str = "/"
) -> int:
    """Replace managed symlinks with real copies and remove the tracked files."""
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    rels = _select_rels(base_dir, paths)
    if not rels:
        print("nothing to eject")
        return 0

    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    # A managed dir-link covering part of the selection is restored as a whole
    # (its tracked files are copied out, the link and the tracked copies are
    # removed), and its files are skipped by the per-file loop below.
    dir_links: dict[str, str] = {}
    covered: set[str] = set()
    for rel in rels:
        target = relative_to_target(rel, root)
        dir_link = _find_dir_symlink(os.path.dirname(target))
        if dir_link is not None and _is_valid_dir_link(dir_link, base_dir):
            dl_rel = os.path.relpath(os.path.realpath(dir_link), base_dir)
            dir_links.setdefault(dir_link, dl_rel)
            covered.add(rel)
    for dir_link, dl_rel in dir_links.items():
        actions.append(
            Action(
                f"restore {display_path(dir_link)} from {display_path(base_dir)}/{dl_rel} (dir-link)",
                lambda d=dir_link, b=base_dir, r=dl_rel: _apply_eject_dir_link(d, b, r),
            )
        )
    for rel in rels:
        if rel in covered:
            continue
        tracked = os.path.join(base_dir, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            continue
        target = relative_to_target(rel, root)
        if os.path.islink(target) and is_within(base_dir, os.path.realpath(target)):
            actions.append(
                Action(
                    f"move {display_path(tracked)} to {display_path(target)} (replacing the managed symlink)",
                    lambda t=target, k=tracked, b=base_dir: _apply_eject(t, k, b),
                )
            )
        elif not os.path.lexists(target):
            # The managed link is gone (e.g. the target directory was
            # deleted): restore the tracked content at the target.
            actions.append(
                Action(
                    f"move {display_path(tracked)} to {display_path(target)}",
                    lambda t=target, k=tracked, b=base_dir: _apply_eject_missing(
                        t, k, b
                    ),
                )
            )
        else:
            infos.append(
                f"skip {display_path(target)}: not a managed symlink (left untouched)"
            )

    rc = _execute(actions, infos, errors, dry_run)
    return 0 if rc == 2 else rc


def status(
    base_dir: str | None,
    paths: list[str] | None = None,
    root: str = "/",
    remotes: list[str] | None = None,
) -> int:
    """Report the state of tracked files (all of them, or only the given paths).

    With ``--remotes`` (optional host names, or every host when no value),
    report the remote files that differ from their tracked copies instead of
    the local problems.
    """
    if remotes is not None:
        hosts, stray = _split_remote_hosts(remotes)
        return _status_remote(base_dir, paths, root, hosts, stray)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    if paths:
        rels: list[str] = []
        seen: set[str] = set()
        for arg in paths:
            resolved = absolute(arg)
            rel = (
                os.path.relpath(resolved, base_dir)
                if is_within(base_dir, resolved)
                else target_to_relative(resolved, root)
            )
            if rel in seen:
                continue
            seen.add(rel)
            rels.append(rel)
    else:
        rels = iter_tracked_files(base_dir)
        if not rels:
            print("no tracked files")
            return 0

    problems = 0
    reported_dir_links: set[str] = set()
    for rel in rels:
        tracked = os.path.join(base_dir, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            print(f"not tracked {display_path(relative_to_target(rel, root))}")
            continue
        problems += _status_one(rel, base_dir, root, reported_dir_links)
    if problems:
        print()
        print("hint: run 'myfiles fix' to resolve these problems interactively")
    return 1 if problems else 0


def _status_state(
    rel: str, base_dir: str, root: str, reported_dir_links: set[str]
) -> tuple[str, str, str] | None:
    """Return (label, target, arrow) for a problem, or ``None`` if healthy.

    ``target`` is the path to display; ``arrow`` is the resolved path shown
    after ``->`` for symlink problems (empty for the others).
    """
    tracked = os.path.join(base_dir, rel)
    target = relative_to_target(rel, root)
    dir_link = _find_dir_symlink(os.path.dirname(target))
    if dir_link is not None:
        if dir_link in reported_dir_links:
            return None
        reported_dir_links.add(dir_link)
        if _is_valid_dir_link(dir_link, base_dir):
            return None  # healthy: served through a managed dir-link
        return (
            ("dangling" if not os.path.exists(dir_link) else "foreign"),
            dir_link,
            os.path.realpath(dir_link),
        )
    if os.path.islink(target):
        resolved = os.path.realpath(target)
        if is_within(base_dir, resolved):
            if resolved == os.path.realpath(tracked):
                return None  # ok: hidden
            return ("elsewhere", target, os.path.realpath(target))
        return (
            ("dangling" if not os.path.exists(target) else "foreign"),
            target,
            os.path.realpath(target),
        )
    if os.path.lexists(target):
        if os.path.isdir(target):
            return ("directory", target, "")
        if same_content(target, tracked):
            return ("not-linked", target, "")
        return ("drift", target, "")
    return ("missing", target, "")


def _status_one(
    rel: str, base_dir: str, root: str, reported_dir_links: set[str]
) -> int:
    """Print the status of one tracked file; return 1 if there is a problem.

    Healthy managed symlinks are hidden (they would be ``ok``).
    """
    state = _status_state(rel, base_dir, root, reported_dir_links)
    if state is None:
        return 0
    label, target, arrow = state
    suffix = f" -> {display_path(arrow)}" if arrow else ""
    print(f"{label:<12}{display_path(target)}{suffix}")
    return 1


def _fix_actions(
    label: str,
    target: str,
    rels: list[str],
    base_dir: str,
    root: str,
    choice: str,
) -> tuple[list[Action], list[str], list[str]]:
    """Build the (deduplicated) actions for one ``fix`` choice (deploy/capture)."""
    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    if choice == "deploy":
        for rel in rels:
            _plan_deploy_one(
                relative_to_target(rel, root),
                rel,
                base_dir,
                True,
                actions,
                infos,
                errors,
            )
    elif choice == "capture":
        capture_plan: list[tuple[str, str, bool]] = []
        for rel in rels:
            _plan_capture(
                base_dir,
                relative_to_target(rel, root),
                [],
                True,
                capture_plan,
                infos,
                errors,
                root,
                [],
            )
        actions.extend(_plan_capture_tail(base_dir, capture_plan, infos, errors, root))
    return _dedupe_actions(actions), infos, errors


def fix(
    base_dir: str | None,
    dry_run: bool,
    paths: list[str] | None = None,
    root: str = "/",
    defaults: bool = False,
    only: list[str] | None = None,
    remotes: list[str] | None = None,
) -> int:
    """Resolve the status problems, one by one.

    Interactively, for each problem the user picks an action among the ones
    adapted to the problem type: ``deploy`` (tracked file is the authority),
    ``capture`` (the system file becomes the tracked one) or ``skip``. ``drift``
    has no default (an explicit choice is required); ``diff`` shows the
    difference first. Each chosen change is confirmed (``[Y/n]``) and applied
    immediately, item by item, like running the corresponding command by hand;
    interrupting with Ctrl-C keeps the already-applied items.

    With ``paths``, only the given file(s)/directory(ies) are processed; with
    ``only``, only the problems of the given type(s) (e.g. ``dangling``,
    ``drift``). ``defaults`` runs non-interactively and applies the default
    action of every problem (problems without a default — ``drift`` — and
    non-auto-fixable ones are skipped). With ``--remotes`` (optional host
    names, or every host when no value), the remote differences are fixed
    instead (deploy/capture/diff per file).
    """
    if remotes is not None:
        hosts, stray = _split_remote_hosts(remotes)
        return _fix_remote(base_dir, dry_run, paths, root, defaults, hosts, stray)
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    problems = _list_problems(base_dir, root)
    if paths:
        selected = set(_select_rels(base_dir, paths))
        problems = [p for p in problems if any(r in selected for r in p[2])]
    if only:
        only_set = set(only)
        problems = [p for p in problems if p[0] in only_set]
    if not problems:
        print("no problems to fix")
        return 0

    rc = 0
    if defaults:
        for label, target, rels in problems:
            _allowed, default = _fix_options(label)
            if default is None:
                print(f"skip {display_path(target)}: {label} (no default)")
                continue
            if default == "skip":
                print(f"skip {display_path(target)}: {label} (not auto-fixable)")
                continue
            actions, infos, errors = _fix_actions(
                label, target, rels, base_dir, root, default
            )
            if _execute(actions, infos, errors, dry_run, auto_confirm=True) == 1:
                rc = 1
        return rc

    first = True
    try:
        for label, target, rels in problems:
            if not first:
                print()
            first = False
            allowed, default = _fix_options(label)
            if allowed == ["skip"]:
                print(f"skip {display_path(target)}: {label} (not auto-fixable)")
                continue
            while True:
                choice = _ask_fix(label, target, allowed, default)
                if choice == "diff":
                    _run_diff(
                        os.path.join(base_dir, rels[0]),
                        relative_to_target(rels[0], root),
                    )
                    continue
                break
            if choice == "skip":
                continue
            actions, infos, errors = _fix_actions(
                label, target, rels, base_dir, root, choice
            )
            if _execute(actions, infos, errors, dry_run, abort_on_interrupt=True) == 1:
                rc = 1
    except _FixAborted:
        print("aborted (changes already applied are kept)")
        return 130
    return rc


def _list_problems(base_dir: str, root: str) -> list[tuple[str, str, list[str]]]:
    """Return [(label, target, rels), ...] for the current status problems.

    A directory symlink problem groups every tracked file underneath it, so a
    single ``deploy`` can replace the directory and link all its files.
    """
    problems: list[tuple[str, str, list[str]]] = []
    reported_dir_links: set[str] = set()
    rels = iter_tracked_files(base_dir)
    for rel in rels:
        tracked = os.path.join(base_dir, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            continue
        dir_link = _find_dir_symlink(os.path.dirname(relative_to_target(rel, root)))
        state = _status_state(rel, base_dir, root, reported_dir_links)
        if state is None:
            continue
        label, target, _arrow = state
        if dir_link is not None:
            affected = [
                r
                for r in rels
                if _find_dir_symlink(os.path.dirname(relative_to_target(r, root)))
                == dir_link
            ]
            problems.append((label, target, affected))
        else:
            problems.append((label, target, [rel]))
    return problems


def _fix_options(label: str) -> tuple[list[str], str | None]:
    """Return (allowed actions, default) for a problem label.

    ``default`` is ``None`` when an explicit choice is required (no safe
    default); ``skip`` is the default when the problem is not auto-fixable.
    """
    if label == "drift":
        # No default: the system file and the tracked one disagree, the user
        # must explicitly choose which is authoritative (deploy = tracked,
        # capture = system).
        return (["deploy", "capture", "diff", "skip"], None)
    if label == "directory":
        return (["skip"], "skip")
    return (["deploy", "skip"], "deploy")


class _FixAborted(Exception):
    """Raised when the user interrupts the interactive ``fix`` loop (Ctrl-C)."""


def _ask_fix(label: str, target: str, allowed: list[str], default: str | None) -> str:
    """Ask which action to take for one problem; return a choice from ``allowed``.

    Empty input picks ``default`` when set; without a default it re-asks (and
    prints a hint). ``EOFError``/``KeyboardInterrupt`` abort the whole
    session by raising ``_FixAborted``.
    """
    keys = {"deploy": "d", "capture": "c", "diff": "i", "skip": "s"}
    # Show the default action first, then the others in their given order.
    ordered = (
        [default, *(a for a in allowed if a != default)]
        if default is not None
        else list(allowed)
    )
    parts: list[str] = []
    for action in ordered:
        text = f"{keys[action]}={action}"
        if action == default:
            text += " (default)"
        parts.append(text)
    prompt = f"{label:<12}{display_path(target)}\n[{', '.join(parts)}] "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError, KeyboardInterrupt:
            raise _FixAborted
        if not answer:
            if default is not None:
                return default
            print(f"(choose one of: {', '.join(allowed)})")
            continue
        for action in allowed:
            if answer in (action, keys[action]):
                return action
        print(f"(choose one of: {', '.join(allowed)})")


def _dedupe_actions(actions: list[Action]) -> list[Action]:
    """Drop duplicate actions (same description), keeping the first occurrence."""
    seen: set[str] = set()
    result: list[Action] = []
    for action in actions:
        if action.description in seen:
            continue
        seen.add(action.description)
        result.append(action)
    return result


def diff(base_dir: str | None, path: str, root: str = "/") -> int:
    """Compare a system file with its tracked copy (like ``diff -Naur``).

    The two sides are ordered by modification date: the older file is the
    ``before`` (``-``) side and the newer one the ``after`` (``+``) side (the
    tracked copy stays ``before`` when the dates are equal or unknown). A
    ``host:/path`` (or a tracked path under ``remotes/``) compares a tracked
    remote file with its remote copy, which is first downloaded into a
    temporary directory.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    parsed = remote.parse_remote_arg(path)
    if parsed is not None:
        host, remote_path = parsed
        rel = remote.remote_rel(remote_path)
        return _run_remote_diff(host, remote_path, _remote_tracked(base_dir, host, rel))
    resolved = absolute(path)
    if is_within(remote.remotes_dir_for_base(base_dir), resolved):
        host, _, sub = os.path.relpath(
            resolved, remote.remotes_dir_for_base(base_dir)
        ).partition("/")
        return _run_remote_diff(host, "/" + sub, _remote_tracked(base_dir, host, sub))
    if is_within(base_dir, resolved):
        # The path is a tracked file inside the base directory.
        tracked = resolved
        rel = os.path.relpath(resolved, base_dir)
        target = relative_to_target(rel, root)
    else:
        # The path is a system target path.
        target = resolved
        rel = os.path.relpath(resolved, root)
        tracked = os.path.join(base_dir, rel)
    return _run_diff(tracked, target)


def _order_by_mtime(
    a: str, b: str, label_a: str, label_b: str
) -> tuple[str, str, str, str]:
    """Return ``(before, after, label_before, label_after)`` ordered by mtime.

    The older file (smallest mtime) becomes the ``-``/``before`` side and the
    newer file the ``+``/``after`` side. When the dates are equal or cannot be
    read (a file is missing...), the original order (``a`` before ``b``) is
    kept.
    """
    try:
        a_mtime = os.path.getmtime(a)
        b_mtime = os.path.getmtime(b)
    except OSError:
        return a, b, label_a, label_b
    if a_mtime > b_mtime:
        return b, a, label_b, label_a
    return a, b, label_a, label_b


def _run_diff(tracked: str, target: str) -> int:
    before, after, label_before, label_after = _order_by_mtime(
        tracked, target, tracked, target
    )
    return _run_diff_with_labels(before, after, label_before, label_after)


def _run_diff_with_labels(
    before: str, after: str, label_before: str, label_after: str
) -> int:
    try:
        result = subprocess.run(
            [
                "diff",
                "-Naur",
                f"--label={label_before}",
                f"--label={label_after}",
                before,
                after,
            ],
            check=False,
        )
    except FileNotFoundError, OSError:
        print("error: the `diff` command is not available")
        return 1
    return result.returncode


# --------------------------------------------------------------------------- #
# capture helpers
# --------------------------------------------------------------------------- #


def _plan_capture(
    base_dir: str,
    arg: str,
    ignore: list[str],
    force: bool,
    plan: list[tuple[str, str, bool]],
    infos: list[str],
    errors: list[str],
    root: str,
    extra_actions: list[Action],
) -> None:
    raw = absolute(arg)

    if os.path.islink(raw):
        resolved = os.path.realpath(raw)
        if is_within(base_dir, resolved):
            # Already managed (a file symlink or a dir-link): idempotent no-op.
            infos.append(f"skip (already captured): {display_path(raw)}")
            return
        raw = resolved
    else:
        raw = os.path.realpath(raw)

    if not os.path.lexists(raw):
        errors.append(f"{arg} does not exist")
        return
    if is_within(base_dir, raw):
        errors.append(
            f"{arg} is inside the base directory ({display_path(base_dir)}); "
            "capturing from the base directory is not allowed"
        )
        return
    if raw == "/":
        errors.append("capturing the filesystem root is not allowed")
        return

    if os.path.isdir(raw):
        # A directory is always captured as a dir-link; the `--ignore` entries
        # are appended to the repository's `.gitignore` (see _plan_capture_dir).
        _plan_capture_dir(
            base_dir, raw, force, ignore, infos, errors, root, extra_actions
        )
    else:
        _plan_file(base_dir, raw, target_to_relative(raw, root), force, plan, errors)


def _plan_capture_dir(
    base_dir: str,
    src: str,
    force: bool,
    ignore: list[str],
    infos: list[str],
    errors: list[str],
    root: str,
    extra_actions: list[Action],
) -> None:
    """Plan capturing ``src`` (a real directory) as a dir-link.

    A fully-managed directory (every entry is a correctly-linked managed
    symlink or a regular file identical to its tracked copy, zero drift) is
    converted in place: the directory is removed and replaced by a dir-link,
    its tracked files are already in ``base-dir``. A fresh directory is moved
    into ``base-dir`` and linked back. Drift (a file whose content differs, a
    foreign/misplaced symlink, a tracked file missing) is refused unless
    ``force`` (then the system content wins and becomes the new tracked copy).
    ``--ignore`` entries never count as drift; the ones already on disk are
    moved into ``base-dir`` during the conversion (so they keep working) and
    are appended to the repository's ``.gitignore`` (never versioned).
    """
    rel = target_to_relative(src, root)
    dest = os.path.join(base_dir, rel)
    if os.path.lexists(dest) and not os.path.isdir(dest):
        errors.append(
            f"{display_path(dest)} already exists and is not a directory; "
            "cannot capture the directory as a dir-link"
        )
        return
    ignored_entries = list(_iter_ignored(src, ignore, base_dir))
    for entry in ignored_entries:
        infos.append(f"skip (ignored): {display_path(entry)}")
    if os.path.isdir(dest):
        drift = _dir_drift(src, base_dir, dest, ignore)
        if drift is None:
            extra_actions.append(
                Action(
                    f"convert to dir-link {display_path(src)} -> {display_path(base_dir)}/{rel}",
                    lambda s=src, b=base_dir, r=rel, i=ignore: (
                        _apply_convert_to_dir_link(s, b, r, i)
                    ),
                )
            )
        elif not force:
            if os.path.islink(drift):
                reason = "is not a correctly managed symlink"
            else:
                reason = "differs from its tracked copy"
            errors.append(
                f"{display_path(src)} is not fully managed: {display_path(drift)} "
                f"{reason} (drift). Run `myfiles fix` to resolve the drift or "
                "use --force to adopt the system content"
            )
            return
        else:
            extra_actions.append(
                Action(
                    f"capture {display_path(src)} -> {display_path(base_dir)}/{rel} as a dir-link (forced: system content wins)",
                    lambda s=src, d=dest, b=base_dir, r=rel: _apply_capture_dir_force(
                        s, d, b, r
                    ),
                )
            )
    else:
        extra_actions.append(
            Action(
                f"move-and-link {display_path(src)} -> {display_path(base_dir)}/{rel} (dir-link)",
                lambda s=src, d=dest, t=src, b=base_dir, r=rel: (
                    _apply_move_and_link_dir(s, d, t, b, r)
                ),
            )
        )
    if ignored_entries:
        gitignore = _gitignore_action(
            base_dir,
            [
                os.path.join(BASE_DIR_NAME, target_to_relative(entry, root))
                for entry in ignored_entries
            ],
        )
        if gitignore is not None:
            extra_actions.append(gitignore)


def _dir_drift(path: str, base_dir: str, dest: str, ignore: list[str]) -> str | None:
    """Return the first non-managed entry under ``path``, or ``None``.

    An entry is non-managed when it is a real file whose content differs from
    its tracked copy (drift), a symlink that does not resolve to its tracked
    location under ``dest`` (foreign or misplaced), or a managed link whose
    tracked file is missing. A real file identical to its tracked copy is fine
    (the dir-link will serve the identical tracked copy); entries matching
    ``ignore`` are excluded from the check. Real subdirectories are walked
    recursively.
    """
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        kept: list[str] = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if _excluded(os.path.relpath(full, path), ignore, is_dir=True):
                continue  # ignored directory: excluded (moved into base-dir)
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path)
            if _excluded(rel, ignore, is_dir=False):
                continue  # ignored file: excluded (moved into base-dir)
            expected = os.path.join(dest, rel)
            if not os.path.islink(full):
                # A real file is drift only when it differs from the tracked
                # copy: an identical file is safe (the dir-link serves the
                # identical tracked copy).
                if os.path.exists(expected) and same_content(full, expected):
                    continue
                return full
            if not os.path.exists(expected) or os.path.realpath(
                full
            ) != os.path.realpath(expected):
                return full
        for name in dirnames:
            full = os.path.join(dirpath, name)
            expected = os.path.join(dest, os.path.relpath(full, path))
            if not os.path.islink(full):
                continue  # real subdirectory: walked recursively
            if not os.path.exists(expected) or os.path.realpath(
                full
            ) != os.path.realpath(expected):
                return full
    return None


def _iter_ignored(path: str, ignore: list[str], base_dir: str) -> Iterator[str]:
    """Yield the ignored entries under ``path`` (managed symlinks excluded).

    Entries matching ``ignore`` are ignored; a symlink that already resolves
    into ``base-dir`` (a managed symlink) is *not* ignored — its tracked file
    is already in place, so it must not be moved or gitignored.
    """
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        kept: list[str] = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, path)
            managed = os.path.islink(full) and is_within(
                base_dir, os.path.realpath(full)
            )
            if _excluded(rel, ignore, is_dir=True) and not managed:
                yield full
            else:
                kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path)
            managed = os.path.islink(full) and is_within(
                base_dir, os.path.realpath(full)
            )
            if _excluded(rel, ignore, is_dir=False) and not managed:
                yield full


def _move_ignored(src: str, dest: str, base_dir: str, ignore: list[str]) -> None:
    """Move the entries under ``src`` matching ``ignore`` into ``dest``.

    They are relocated (not deleted) so they keep working through the dir-link;
    gitignore them with ``myfiles ignore`` so they are never versioned.
    Managed symlinks are left alone.
    """
    for full in list(_iter_ignored(src, ignore, base_dir)):
        rel = os.path.relpath(full, src)
        dd = os.path.join(dest, rel)
        _makedirs(os.path.dirname(dd))
        if os.path.lexists(dd):
            if os.path.isdir(dd) and not os.path.islink(dd):
                _rmtree(dd)
            else:
                _remove(dd)
        _move(full, dd)


def _plan_file(
    base_dir: str,
    real_src: str,
    rel: str,
    force: bool,
    plan: list[tuple[str, str, bool]],
    errors: list[str],
) -> None:
    dest = os.path.join(base_dir, rel)
    if os.path.isdir(dest):
        errors.append(f"{dest} is a directory")
        return
    if os.path.lexists(dest):
        if same_content(real_src, dest):
            plan.append((real_src, rel, True))
        elif force:
            if is_dirty(base_dir, rel):
                errors.append(
                    f"{dest} has uncommitted changes in git; commit or restore it before using --force"
                )
            else:
                plan.append((real_src, rel, False))
        else:
            errors.append(
                f"{dest} already exists and differs; use --force to overwrite"
            )
    else:
        plan.append((real_src, rel, False))


#: Cache of parsed gitignore specs, keyed by the pattern tuple.
_IGNORE_SPECS: dict[tuple[str, ...], pathspec.PathSpec[Any]] = {}


def _ignored_spec(patterns: list[str]) -> pathspec.PathSpec[Any]:
    """Return the gitignore spec for ``patterns`` (parsed once, then cached)."""
    key = tuple(patterns)
    spec = _IGNORE_SPECS.get(key)
    if spec is None:
        spec = cast(
            pathspec.PathSpec[Any], pathspec.PathSpec.from_lines("gitignore", patterns)
        )
        _IGNORE_SPECS[key] = spec
    return spec


def _excluded(rel: str, patterns: list[str], is_dir: bool) -> bool:
    """Return ``True`` if ``rel`` (relative to the walked source) is ignored.

    ``--ignore`` patterns follow gitignore syntax: ``cache`` matches the
    directory at any depth, ``*.log`` any ``.log`` basename, ``cache/**``
    everything under ``cache``, a leading ``/`` anchors to the captured
    directory root, a trailing ``/`` restricts to directories.
    """
    if not patterns:
        return False
    path = rel + "/" if is_dir else rel
    return _ignored_spec(patterns).match_file(path)


# --------------------------------------------------------------------------- #
# deploy / eject helpers
# --------------------------------------------------------------------------- #


def _path_to_rel(base_dir: str, path: str) -> str:
    """Map a user-supplied path to a base-dir-relative path (``rel``).

    Accepts a tracked path inside ``base_dir`` (absolute), an absolute target
    path (under the root), or a root-relative path.
    """
    resolved = absolute(path)
    if is_within(base_dir, resolved):
        return os.path.relpath(resolved, base_dir)
    return _normalize_rel(path)


def _select_rels(base_dir: str, paths: list[str]) -> list[str]:
    if not paths:
        return iter_tracked_files(base_dir)
    rels: set[str] = set()
    for path in paths:
        rel = _path_to_rel(base_dir, path)
        full = os.path.join(base_dir, rel)
        if os.path.isdir(full):
            prefix = rel + "/"
            rels.update(r for r in iter_tracked_files(base_dir) if r.startswith(prefix))
        else:
            rels.add(rel)
    return sorted(rels)


def _normalize_rel(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path).lstrip("/"))


def _plan_deploy_one(
    target: str,
    rel: str,
    base_dir: str,
    force: bool,
    actions: list[Action],
    infos: list[str],
    errors: list[str],
) -> None:
    parent = os.path.dirname(target)
    dir_link = _find_dir_symlink(parent)
    if dir_link is not None and _is_valid_dir_link(dir_link, base_dir):
        # The target is already served through a valid managed dir-link.
        infos.append(f"skip (linked via dir-link): {display_path(target)}")
        return
    if not _plan_dir_symlinks(parent, base_dir, actions, errors, force=force):
        errors.append(
            f"cannot deploy {display_path(target)}: an intermediate path is a foreign symlink"
        )
        return

    tracked = os.path.join(base_dir, rel)

    if os.path.islink(target):
        real = os.path.realpath(target)
        if is_within(base_dir, real):
            if real == os.path.realpath(tracked):
                infos.append(f"skip (already linked): {display_path(target)}")
            elif force:
                actions.append(
                    _link_action(target, base_dir, rel, reason="misplaced symlink")
                )
            else:
                errors.append(
                    f"{display_path(target)} is a managed symlink pointing elsewhere; use --force to re-link it"
                )
        else:
            if force:
                actions.append(
                    _link_action(target, base_dir, rel, reason="foreign symlink")
                )
            else:
                errors.append(
                    f"{display_path(target)} is a {_foreign_symlink_detail(target)}; use --force to replace it"
                )
        return

    if os.path.isdir(target):
        errors.append(f"{display_path(target)} is a directory")
        return

    if os.path.isfile(target):
        if same_content(target, tracked):
            actions.append(
                _link_action(target, base_dir, rel, reason="identical content")
            )
        elif force:
            actions.append(
                _link_action(
                    target,
                    base_dir,
                    rel,
                    reason="different content, forced",
                    backup=True,
                )
            )
        else:
            errors.append(
                f"{display_path(target)} exists and differs; run `diff {display_path(target)} {display_path(tracked)}`"
            )
        return

    actions.append(_link_action(target, base_dir, rel))


def _is_valid_dir_link(path: str, base_dir: str) -> bool:
    """Return ``True`` if ``path`` is a symlink resolving into an existing base-dir location."""
    if not os.path.islink(path):
        return False
    resolved = os.path.realpath(path)
    return is_within(base_dir, resolved) and os.path.exists(resolved)


def _find_dir_symlink(parent: str) -> str | None:
    """Return the first directory component of ``parent`` that is a symlink."""
    current = ""
    for component in [c for c in parent.split("/") if c]:
        current += "/" + component
        if os.path.islink(current):
            return current
    return None


def _foreign_symlink_detail(target: str) -> str:
    """Describe a foreign symlink, flagging it as ``dangling`` when its target is missing."""
    detail = "foreign symlink"
    if not os.path.exists(target):
        detail += ", dangling"
    return detail


def _plan_dir_symlinks(
    parent: str,
    base_dir: str,
    actions: list[Action],
    errors: list[str],
    force: bool = False,
) -> bool:
    """Plan replacing directory symlinks in ``parent`` with real directories.

    A directory symlink into ``base_dir`` is always replaced; a foreign one is
    only replaced with ``force`` (otherwise ``False`` is returned).
    """
    current = ""
    for component in [c for c in parent.split("/") if c]:
        current += "/" + component
        if os.path.islink(current):
            if _is_valid_dir_link(current, base_dir):
                continue  # valid managed dir-link: leave it alone
            resolved = os.path.realpath(current)
            if not is_within(base_dir, resolved) and not force:
                return False
            actions.append(
                Action(
                    f"replace directory symlink {display_path(current)} with a real directory",
                    lambda c=current: _apply_replace_dir_symlink(c),
                )
            )
    return True


def _apply_replace_dir_symlink(path: str) -> None:
    """Replace a directory symlink with a real (empty) directory. Idempotent."""
    if os.path.islink(path):
        _unlink(path)
    if not os.path.isdir(path):
        _mkdir(path)


def _sudo(cmd: list[str]) -> None:
    """Run ``cmd`` via ``sudo`` (fallback for root-owned paths)."""
    print(f"(permission denied — retrying with sudo: sudo {' '.join(cmd)})")
    result = subprocess.run(["sudo", *cmd], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"`sudo {' '.join(cmd)}` failed")


def _fs_apply(direct: Callable[[], object], sudo_cmd: list[str]) -> None:
    """Run ``direct``; on ``PermissionError``, retry the operation via ``sudo``."""
    try:
        direct()
    except PermissionError:
        try:
            _sudo(sudo_cmd)
        except FileNotFoundError:
            print(
                "error: the operation requires root but `sudo` is not available; "
                "run the command as root instead"
            )
            raise


def _makedirs(path: str) -> None:
    _fs_apply(lambda: os.makedirs(path, exist_ok=True), ["mkdir", "-p", path])


def _remove(path: str) -> None:
    _fs_apply(lambda: os.remove(path), ["rm", "-f", path])


def _rmtree(path: str) -> None:
    _fs_apply(lambda: shutil.rmtree(path), ["rm", "-rf", path])


def _unlink(path: str) -> None:
    _fs_apply(lambda: os.unlink(path), ["rm", "-f", path])


def _replace(src: str, dest: str) -> None:
    _fs_apply(lambda: os.replace(src, dest), ["mv", "-f", src, dest])


def _move(src: str, dest: str) -> None:
    _fs_apply(lambda: shutil.move(src, dest), ["mv", "-f", src, dest])


def _copyfile(src: str, dest: str) -> None:
    _fs_apply(lambda: shutil.copyfile(src, dest), ["cp", src, dest])


def _symlink(link: str, target: str) -> None:
    _fs_apply(lambda: os.symlink(link, target), ["ln", "-s", link, target])


def _mkdir(path: str) -> None:
    _fs_apply(lambda: os.mkdir(path), ["mkdir", path])


def _link_action(
    target: str,
    base_dir: str,
    rel: str,
    reason: str | None = None,
    backup: bool = False,
) -> Action:
    """Plan creating a symlink at ``target`` pointing at the tracked file.

    Links point directly at ``base_dir``/``rel`` (no indirection). When an
    existing file at the target is converted into the symlink, the ``reason``
    is shown as ``(replace: <reason>)``; ``backup`` keeps the original as
    ``target.bak``.
    """
    link = os.path.join(base_dir, rel)

    def apply() -> None:
        _makedirs(os.path.dirname(target))
        if reason is not None and os.path.lexists(target):
            if backup:
                _replace(target, target + ".bak")
            else:
                _remove(target)
        _symlink(link, target)

    note = ""
    if reason is not None:
        note = f" (replace: {reason}"
        if backup:
            note += f"; backup saved as {display_path(target + '.bak')}"
        note += ")"
    return Action(
        f"link {display_path(target)} -> {display_path(base_dir)}/{rel}{note}",
        apply,
    )


def _apply_move(src: str, dest: str) -> None:
    _makedirs(os.path.dirname(dest))
    if os.path.lexists(dest):
        _remove(dest)
    _move(src, dest)


def _apply_move_and_link(
    src: str, dest: str, target: str, base_dir: str, rel: str
) -> None:
    """Move ``src`` into the base directory, then link it back at ``target``."""
    _apply_move(src, dest)
    _link_action(target, base_dir, rel).apply()


def _link_action_dir(target: str, base_dir: str, rel: str) -> Action:
    """Plan creating a directory symlink at ``target`` pointing at ``base_dir/rel``."""

    def apply() -> None:
        _makedirs(os.path.dirname(target))
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                _rmtree(target)
            else:
                _remove(target)
        _symlink(os.path.join(base_dir, rel), target)

    return Action(
        f"link {display_path(target)} -> {display_path(base_dir)}/{rel} (dir-link)",
        apply,
    )


def _apply_move_and_link_dir(
    src: str, dest: str, target: str, base_dir: str, rel: str
) -> None:
    """Move a whole directory into ``base-dir``, then link it back as a dir-link."""
    _apply_move(src, dest)
    _link_action_dir(target, base_dir, rel).apply()


def _apply_convert_to_dir_link(
    target: str, base_dir: str, rel: str, ignore: list[str]
) -> None:
    """Remove a fully-managed directory and replace it with a dir-link.

    ``--ignore`` entries under ``target`` are moved into ``base-dir`` first so
    they keep working through the dir-link (gitignore them with
    ``myfiles ignore``).
    """
    _move_ignored(target, os.path.join(base_dir, rel), base_dir, ignore)
    _rmtree(target)
    _link_action_dir(target, base_dir, rel).apply()


def _apply_capture_dir_force(src: str, dest: str, base_dir: str, rel: str) -> None:
    """Adopt the system directory content into ``base-dir``, then convert.

    Real files under ``src`` overwrite the tracked files in ``dest`` (system
    content wins); managed symlinks are left alone (their tracked files are
    already in ``dest``).
    """
    for dirpath, _dirnames, filenames in os.walk(src, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue  # managed symlink: tracked file already in dest
            sub = os.path.relpath(full, src)
            dd = os.path.join(dest, sub)
            _makedirs(os.path.dirname(dd))
            if os.path.lexists(dd):
                _remove(dd)
            _copyfile(full, dd)
    _rmtree(src)
    _link_action_dir(src, base_dir, rel).apply()


def _apply_eject_dir_link(dir_link: str, base_dir: str, rel: str) -> None:
    """Restore a managed dir-link: copy the tracked files out, then remove the link and its tracked copies."""
    _unlink(dir_link)
    prefix = rel + "/"
    for sub in iter_tracked_files(base_dir):
        if not sub.startswith(prefix):
            continue
        src = os.path.join(base_dir, sub)
        dest = os.path.join(dir_link, os.path.relpath(sub, rel))
        _makedirs(os.path.dirname(dest))
        _copyfile(src, dest)
        _remove(src)
    prune_empty_dirs(os.path.join(base_dir, rel), base_dir)


def _apply_eject(target: str, tracked: str, base_dir: str) -> None:
    _unlink(target)
    _copyfile(tracked, target)
    _remove(tracked)
    prune_empty_dirs(os.path.dirname(tracked), base_dir)


def _apply_eject_missing(target: str, tracked: str, base_dir: str) -> None:
    """Restore a tracked file at its (missing) target, then remove it."""
    _makedirs(os.path.dirname(target))
    _copyfile(tracked, target)
    _remove(tracked)
    prune_empty_dirs(os.path.dirname(tracked), base_dir)


# --------------------------------------------------------------------------- #
# remote helpers (remotes/<host> mirrored via SSH, no symlinks)
# --------------------------------------------------------------------------- #


def _remote_tracked(base_dir: str, host: str, rel: str) -> str:
    """Return the tracked path of a remote file under ``remotes/<host>``."""
    return os.path.join(remote.host_dir(base_dir, host), rel)


def _capture_remote_paths(
    base_dir: str, sel: list[tuple[str, str]], dry_run: bool
) -> int:
    """Capture remote files/directories given as ``host:/path``.

    The remote content is copied into ``remotes/<host>`` (only files that
    differ); there are no symlinks on a remote host.
    """
    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    for host, remote_path in sel:
        if not remote.remote_lexists(host, remote_path):
            errors.append(f"{remote.format_remote(host, remote_path)} does not exist")
            continue
        rel = remote.remote_rel(remote_path)
        tracked = _remote_tracked(base_dir, host, rel)
        if remote.remote_isdir(host, remote_path):
            try:
                files = remote.remote_walk(host, remote_path)
            except remote.RemoteError as exc:
                errors.append(str(exc))
                continue
            for f in files:
                sub = os.path.relpath(f, remote_path)
                _plan_remote_download(
                    host, f, os.path.join(tracked, sub), actions, infos
                )
        else:
            _plan_remote_download(host, remote_path, tracked, actions, infos)
    rc = _execute(actions, infos, errors, dry_run)
    return 0 if rc == 2 else rc


def _capture_remote_scan(base_dir: str, hosts: list[str], dry_run: bool) -> int:
    """Recapture the known remote files of the given hosts (or all hosts).

    A known file (a file under ``remotes/<host>``) is copied back from the host
    only when its content differs.
    """
    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    known = remote.iter_remote_files(base_dir)
    if hosts:
        errors.extend(_validate_remote_hosts(base_dir, hosts))
        wanted = set(hosts)
        known = [(h, r) for h, r in known if h in wanted]
    if not known and not errors:
        print("nothing to capture")
        return 0
    for host, rel in known:
        remote_path = "/" + rel
        tracked = _remote_tracked(base_dir, host, rel)
        if not remote.remote_lexists(host, remote_path):
            infos.append(
                f"skip (not on remote): {remote.format_remote(host, remote_path)}"
            )
            continue
        _plan_remote_download(host, remote_path, tracked, actions, infos)
    rc = _execute(actions, infos, errors, dry_run)
    return 0 if rc == 2 else rc


def _plan_remote_download(
    host: str,
    remote_path: str,
    tracked: str,
    actions: list[Action],
    infos: list[str],
) -> None:
    """Plan copying a remote file into the repo (only when the content differs)."""
    remote_h = remote.remote_hash(host, remote_path)
    if remote_h is None:
        infos.append(f"skip (not on remote): {remote.format_remote(host, remote_path)}")
        return
    local_h = hash_file(tracked) if os.path.isfile(tracked) else None
    if remote_h == local_h:
        infos.append(f"skip (identical): {remote.format_remote(host, remote_path)}")
        return
    actions.append(
        Action(
            f"copy {remote.format_remote(host, remote_path)} -> {display_path(tracked)}",
            lambda h=host, r=remote_path, t=tracked: _apply_remote_download(h, r, t),
        )
    )


def _plan_remote_upload(
    host: str,
    remote_path: str,
    tracked: str,
    actions: list[Action],
    infos: list[str],
) -> None:
    """Plan copying a tracked remote file to the host (only when it differs)."""
    local_h = hash_file(tracked)
    if remote.remote_hash(host, remote_path) == local_h:
        infos.append(f"skip (identical): {remote.format_remote(host, remote_path)}")
        return
    actions.append(
        Action(
            f"copy {display_path(tracked)} -> {remote.format_remote(host, remote_path)}",
            lambda h=host, r=remote_path, t=tracked: _apply_remote_upload(h, r, t),
        )
    )


def _apply_remote_download(host: str, remote_path: str, tracked: str) -> None:
    _makedirs(os.path.dirname(tracked))
    remote.remote_download(host, remote_path, tracked)


def _apply_remote_upload(host: str, remote_path: str, tracked: str) -> None:
    remote.remote_mkdirs(host, os.path.dirname(remote_path))
    remote.remote_upload(tracked, host, remote_path)


def _remote_sel(
    base_dir: str,
    paths: list[str],
    hosts: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Return the ``(host, rel)`` remote files selected by ``paths`` (all when empty).

    ``hosts`` (when non-empty) restricts the selection to those hosts. Accepts
    ``host:/path``, a path inside the ``remotes/`` directory, or a
    root-relative ``remotes/...`` path; a directory selects everything under it.
    """
    all_files = remote.iter_remote_files(base_dir)
    if hosts:
        wanted = set(hosts)
        all_files = [(h, r) for h, r in all_files if h in wanted]
    if not paths:
        return all_files
    known = all_files
    selected: set[tuple[str, str]] = set()
    for p in paths:
        parsed = remote.parse_remote_arg(p)
        if parsed is not None:
            host, remote_path = parsed
            _remote_sel_one(known, selected, host, remote.remote_rel(remote_path))
            continue
        resolved = absolute(p)
        if is_within(remote.remotes_dir_for_base(base_dir), resolved):
            host, _, sub = os.path.relpath(
                resolved, remote.remotes_dir_for_base(base_dir)
            ).partition("/")
            _remote_sel_one(known, selected, host, sub)
            continue
        norm = os.path.normpath(os.path.expanduser(p).lstrip("/"))
        if norm.startswith(remote.REMOTES_DIR_NAME + "/"):
            host, _, sub = (
                norm[len(remote.REMOTES_DIR_NAME) :].lstrip("/").partition("/")
            )
            _remote_sel_one(known, selected, host, sub)
    return sorted(selected)


def _remote_sel_one(
    known: list[tuple[str, str]],
    selected: set[tuple[str, str]],
    host: str,
    rel: str,
) -> None:
    """Add a ``(host, rel)`` selection to ``selected``, expanding directories."""
    prefix = rel + "/"
    matches = [
        (h, r) for h, r in known if h == host and (r == rel or r.startswith(prefix))
    ]
    if matches:
        selected.update(matches)
    else:
        selected.add((host, rel))


def _deploy_remote(base_dir: str, sel: list[tuple[str, str]], dry_run: bool) -> int:
    """Deploy tracked remote files to the host (copied, only when they differ)."""
    plan: set[tuple[str, str]] = set()
    for host, rel in sel:
        tracked = _remote_tracked(base_dir, host, rel)
        if os.path.isdir(tracked) and not os.path.islink(tracked):
            prefix = rel + "/"
            plan.update(
                (h, r)
                for h, r in remote.iter_remote_files(base_dir)
                if h == host and r.startswith(prefix)
            )
        else:
            plan.add((host, rel))
    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    for host, rel in sorted(plan):
        tracked = _remote_tracked(base_dir, host, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            infos.append(
                f"skip {remote.format_remote(host, '/' + rel)}: not a tracked file"
            )
            continue
        _plan_remote_upload(host, "/" + rel, tracked, actions, infos)
    rc = _execute(actions, infos, errors, dry_run)
    return 0 if rc == 2 else rc


def _run_remote_diff(host: str, remote_path: str, tracked: str) -> int:
    """Diff a tracked remote file against its remote copy (downloaded to a temp dir).

    The two sides are ordered by modification date: the older file is the
    ``-``/``before`` side, the newer one the ``+``/``after`` side (the remote
    file's real mtime is compared, not the temporary download's). When the
    dates are equal or the remote mtime cannot be read, the tracked copy stays
    the ``before`` side.
    """
    if not remote.remote_lexists(host, remote_path):
        print(f"error: {remote.format_remote(host, remote_path)} does not exist")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        downloaded = os.path.join(tmp, "remote")
        try:
            remote.remote_download(host, remote_path, downloaded)
        except remote.RemoteError as exc:
            print(f"error: {exc}")
            return 1
        tracked_label = display_path(tracked)
        remote_label = remote.format_remote(host, remote_path)
        remote_mtime = remote.remote_mtime(host, remote_path)
        tracked_mtime = os.path.getmtime(tracked) if os.path.isfile(tracked) else None
        if (
            tracked_mtime is not None
            and remote_mtime is not None
            and tracked_mtime > remote_mtime
        ):
            # The tracked copy is newer -> it becomes the ``+``/``after`` side.
            return _run_diff_with_labels(
                downloaded, tracked, remote_label, tracked_label
            )
        return _run_diff_with_labels(tracked, downloaded, tracked_label, remote_label)


def _status_remote(
    base_dir: str | None,
    paths: list[str] | None,
    root: str,
    hosts: list[str] | None = None,
    stray: list[str] | None = None,
) -> int:
    """Report the remote files that differ from their tracked copies.

    ``hosts`` (when non-empty) restricts the report to those hosts; ``stray``
    are positional-looking paths swallowed by ``--remotes``'s ``nargs="*"`` and
    moved back into the selection. For a differing file, the two modification
    dates are shown and the most recent one is marked; a tracked file missing
    on the host is reported as ``missing``. Returns ``1`` when any remote file
    differs.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    if stray:
        paths = (paths or []) + stray
    errors = _validate_remote_hosts(base_dir, hosts or [])
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    problems = 0
    for host, rel in _remote_sel(base_dir, paths or [], hosts=hosts):
        remote_path = "/" + rel
        tracked = _remote_tracked(base_dir, host, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            print(f"not tracked {remote.format_remote(host, remote_path)}")
            problems += 1
            continue
        if not remote.remote_lexists(host, remote_path):
            print(f"missing {remote.format_remote(host, remote_path)}")
            problems += 1
            continue
        if remote.remote_hash(host, remote_path) == hash_file(tracked):
            continue  # identical: hidden
        print(f"drift {remote.format_remote(host, remote_path)}")
        _print_remote_dates(
            os.path.getmtime(tracked), remote.remote_mtime(host, remote_path)
        )
        problems += 1
    if problems:
        print()
        print(
            "hint: run 'myfiles fix --remotes' to resolve these differences "
            "interactively"
        )
    return 1 if problems else 0


def _print_remote_dates(local_m: float, remote_m: float | None) -> None:
    """Print the local/remote modification dates, marking the most recent."""

    def fmt(m: float | None) -> str:
        if m is None:
            return "missing"
        tz = datetime.now().astimezone().tzinfo
        return datetime.fromtimestamp(m, tz).strftime("%Y-%m-%d %H:%M:%S")

    local_s = fmt(local_m)
    remote_s = fmt(remote_m)
    if remote_m is not None and remote_m > local_m:
        remote_s += " (newer)"
    else:
        local_s += " (newer)"
    print(f"    local:  {local_s}")
    print(f"    remote: {remote_s}")


def _fix_remote(
    base_dir: str | None,
    dry_run: bool,
    paths: list[str] | None,
    root: str,
    defaults: bool,
    hosts: list[str] | None = None,
    stray: list[str] | None = None,
) -> int:
    """Resolve the remote differences interactively (deploy/capture/diff per file).

    ``hosts`` (when non-empty) restricts the resolution to those hosts;
    ``stray`` are positional-looking paths swallowed by ``--remotes``'s
    ``nargs="*"`` and moved back into the selection.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    if stray:
        paths = (paths or []) + stray
    errors = _validate_remote_hosts(base_dir, hosts or [])
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    problems = _remote_problems(base_dir, paths or [], hosts=hosts)
    if not problems:
        print("no problems to fix")
        return 0
    rc = 0
    if defaults:
        for label, host, rel in problems:
            _allowed, default = _remote_fix_options(label)
            if default is None:
                print(
                    f"skip {remote.format_remote(host, '/' + rel)}: {label} (no default)"
                )
                continue
            if default == "skip":
                continue
            actions, infos, errors = _remote_fix_actions(
                base_dir, label, host, rel, default
            )
            if _execute(actions, infos, errors, dry_run, auto_confirm=True) == 1:
                rc = 1
        return rc
    first = True
    try:
        for label, host, rel in problems:
            if not first:
                print()
            first = False
            allowed, default = _remote_fix_options(label)
            target = remote.format_remote(host, "/" + rel)
            while True:
                choice = _ask_fix(label, target, allowed, default)
                if choice == "diff":
                    _run_remote_diff(
                        host, "/" + rel, _remote_tracked(base_dir, host, rel)
                    )
                    continue
                break
            if choice == "skip":
                continue
            actions, infos, errors = _remote_fix_actions(
                base_dir, label, host, rel, choice
            )
            if _execute(actions, infos, errors, dry_run, abort_on_interrupt=True) == 1:
                rc = 1
    except _FixAborted:
        print("aborted (changes already applied are kept)")
        return 130
    return rc


def _remote_problems(
    base_dir: str,
    paths: list[str],
    hosts: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``[(label, host, rel), ...]`` for the remote differences.

    ``hosts`` (when non-empty) restricts the scan to those hosts. ``drift``:
    the remote file exists but differs from its tracked copy. ``missing``: the
    tracked file is absent on the host.
    """
    problems: list[tuple[str, str, str]] = []
    for host, rel in _remote_sel(base_dir, paths, hosts=hosts):
        tracked = _remote_tracked(base_dir, host, rel)
        if not os.path.isfile(tracked) or os.path.islink(tracked):
            continue
        remote_path = "/" + rel
        if not remote.remote_lexists(host, remote_path):
            problems.append(("missing", host, rel))
            continue
        if remote.remote_hash(host, remote_path) != hash_file(tracked):
            problems.append(("drift", host, rel))
    return problems


def _remote_fix_options(label: str) -> tuple[list[str], str | None]:
    """Return (allowed actions, default) for a remote problem label."""
    if label == "drift":
        return (["deploy", "capture", "diff", "skip"], None)
    if label == "missing":
        return (["deploy", "skip"], "deploy")
    return (["skip"], "skip")


def _remote_fix_actions(
    base_dir: str, label: str, host: str, rel: str, choice: str
) -> tuple[list[Action], list[str], list[str]]:
    """Build the actions for one remote ``fix`` choice (deploy/capture)."""
    actions: list[Action] = []
    infos: list[str] = []
    errors: list[str] = []
    tracked = _remote_tracked(base_dir, host, rel)
    remote_path = "/" + rel
    if choice == "deploy":
        _plan_remote_upload(host, remote_path, tracked, actions, infos)
    elif choice == "capture":
        _plan_remote_download(host, remote_path, tracked, actions, infos)
    return actions, infos, errors
