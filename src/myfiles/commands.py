"""Implementation of the myfiles commands."""

import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

import pathspec
import xdg_base_dirs

from myfiles.fs import iter_tracked_files, prune_empty_dirs, same_content
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

#: Directory roots allowed by `capture` (see SPEC, "Safety scope for capture").
ALLOWED_DIR_ROOTS: tuple[str, ...] = (
    "/etc",
    "/usr",
    str(xdg_base_dirs.xdg_config_home()),
    str(xdg_base_dirs.xdg_data_home()),
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
) -> int:
    """Move files into ``base_dir``, then deploy symlinks for them."""
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
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
    base_dir: str | None, paths: list[str], force: bool, dry_run: bool, root: str = "/"
) -> int:
    """Create symlinks for the tracked files in ``base_dir``."""
    _announce_dry_run(dry_run)
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
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


def ls(base_dir: str | None, root: str = "/") -> int:
    """List the tracked files as target paths (leading ``/``), one per line.

    Files matching the repository's ``.gitignore`` are not shown: only the
    files git actually versions are listed.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    spec = _repo_gitignore_spec(base_dir)
    for rel in iter_tracked_files(base_dir):
        if spec is not None and spec.match_file(os.path.join(BASE_DIR_NAME, rel)):
            continue  # gitignored: not versioned
        print(relative_to_target(rel, root))
    return 0


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
    base_dir: str | None, paths: list[str] | None = None, root: str = "/"
) -> int:
    """Report the state of tracked files (all of them, or only the given paths)."""
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
    non-auto-fixable ones are skipped).
    """
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

    The tracked copy is the ``before`` side and the system file the ``after``
    side.
    """
    base_dir = resolve_base_dir(base_dir)
    if base_dir is None:
        return 1
    root = absolute(root)
    resolved = absolute(path)
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


def _run_diff(tracked: str, target: str) -> int:
    try:
        result = subprocess.run(["diff", "-Naur", tracked, target], check=False)
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
        if not _allowed_dir(raw, root):
            roots = ", ".join(_allowed_roots(root))
            errors.append(f"directory {arg} is outside the allowed roots ({roots})")
            return
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


def _allowed_roots(root: str) -> list[str]:
    """Return the allowed capture roots, mapped under ``root`` when sandboxed.

    With ``--root-dir``, each real root (e.g. ``/etc``) is also accepted at its
    root-relative location (e.g. ``<root>/etc``), so directory captures work in
    a sandbox while the real paths stay valid.
    """
    if root == "/":
        return list(ALLOWED_DIR_ROOTS)
    roots = list(ALLOWED_DIR_ROOTS)
    for allowed in ALLOWED_DIR_ROOTS:
        mapped = os.path.join(root, allowed.lstrip("/"))
        if mapped not in roots:
            roots.append(mapped)
    return roots


def _allowed_dir(path: str, root: str = "/") -> bool:
    return any(is_within(allowed, path) for allowed in _allowed_roots(root))


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
