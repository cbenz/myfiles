"""Integration tests for the myfiles commands."""

import os
import subprocess
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from myfiles import commands


def _confirm_yes(*args: object, **kwargs: object) -> bool:
    return True


def _confirm_no(*args: object, **kwargs: object) -> bool:
    return False


@pytest.fixture(autouse=True)
def confirm_yes(monkeypatch: MonkeyPatch) -> None:
    """Auto-accept confirmation prompts."""
    monkeypatch.setattr(commands, "_ask_confirmation", _confirm_yes)


def place_tracked(base_dir: Path, target: Path) -> Path:
    """Place a tracked file in the base-dir (``<repo root>/files``), mirroring the absolute ``target`` path."""
    rel = os.path.relpath(os.fspath(target), "/")
    dest = base_dir / "files" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def test_capture_file_deploy_eject_roundtrip(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "etc" / "foo" / "bar.txt"
    source.parent.mkdir(parents=True)
    source.write_text("hello")

    assert commands.capture(str(base_dir), [str(source)], [], False, False) == 0

    tracked = place_tracked(base_dir, source)
    assert tracked.read_text() == "hello"
    assert os.path.islink(source)

    assert commands.status(str(base_dir)) == 0

    assert commands.eject(str(base_dir), [], False) == 0
    assert os.path.isfile(source) and not os.path.islink(source)
    assert source.read_text() == "hello"
    assert not tracked.exists()


def test_capture_directory_ignore_adds_to_gitignore(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    src_dir = tmp_path / "etc" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "keep.txt").write_text("keep")
    (src_dir / "skip.log").write_text("skip")
    cache = src_dir / "cache"
    cache.mkdir()
    (cache / "x").write_text("x")
    nested = src_dir / "sub" / "cache"
    nested.mkdir(parents=True)
    (nested / "y").write_text("y")

    assert (
        commands.capture(
            str(base_dir), [str(src_dir)], ["cache", "*.log"], False, False
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "move-and-link" in out
    assert "skip (ignored)" in out
    # The directory is captured as a dir-link: the whole directory mirrors the
    # repo, ignored entries are moved along (not lost).
    tracked_dir = place_tracked(base_dir, src_dir)
    assert os.path.islink(src_dir)
    assert os.path.realpath(src_dir) == os.path.realpath(tracked_dir)
    assert (tracked_dir / "keep.txt").read_text() == "keep"
    assert (tracked_dir / "skip.log").read_text() == "skip"
    assert (tracked_dir / "cache" / "x").read_text() == "x"
    assert (tracked_dir / "sub" / "cache" / "y").read_text() == "y"
    # The ignored entries were appended to the repository's .gitignore —
    # `cache` matches the directory at any depth (gitignore syntax).
    rel = os.path.relpath(os.fspath(src_dir), "/")
    gitignore = (base_dir / ".gitignore").read_text()
    assert f"files/{rel}/skip.log" in gitignore
    assert f"files/{rel}/cache" in gitignore
    assert f"files/{rel}/sub/cache" in gitignore


def test_capture_ignore_anchored_pattern(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # `/cache` (gitignore anchor) ignores only the top-level cache directory.
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    src_dir = tmp_path / "etc" / "app"
    (src_dir / "sub" / "cache").mkdir(parents=True)
    (src_dir / "cache").mkdir(parents=True)
    (src_dir / "cache" / "x").write_text("x")
    (src_dir / "sub" / "cache" / "y").write_text("y")

    assert (
        commands.capture(str(base_dir), [str(src_dir)], ["/cache"], False, False) == 0
    )
    rel = os.path.relpath(os.fspath(src_dir), "/")
    gitignore = (base_dir / ".gitignore").read_text()
    assert f"files/{rel}/cache" in gitignore
    # The nested sub/cache is NOT matched by the anchored pattern.
    assert f"files/{rel}/sub/cache" not in gitignore


def test_capture_directory_ignore_writes_gitignore_via_cli(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    src_dir = tmp_path / "etc" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "keep.conf").write_text("keep")
    (src_dir / ".zsh_history").write_text("h")
    (src_dir / ".zcompdump").write_text("d")
    (src_dir / "x.pyc").write_text("c")
    cache = src_dir / "__pycache__"
    cache.mkdir()
    (cache / "y.pyc").write_text("y")

    assert (
        commands.capture(
            str(base_dir),
            [str(src_dir)],
            [".zsh_history", ".zcompdump*", "*.pyc", "__pycache__"],
            False,
            False,
            root=str(tmp_path),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "skip (ignored)" in out
    # The directory is captured as a dir-link; everything moved into base-dir.
    tracked_dir = base_dir / "files" / "etc" / "app"
    assert os.path.islink(src_dir)
    assert os.path.realpath(src_dir) == os.path.realpath(tracked_dir)
    assert (tracked_dir / "keep.conf").read_text() == "keep"
    assert (tracked_dir / ".zsh_history").read_text() == "h"
    assert (tracked_dir / ".zcompdump").read_text() == "d"
    assert (tracked_dir / "x.pyc").read_text() == "c"
    assert (tracked_dir / "__pycache__" / "y.pyc").read_text() == "y"
    # The ignored entries were appended to .gitignore (never versioned).
    gitignore = (base_dir / ".gitignore").read_text()
    assert "files/etc/app/.zsh_history" in gitignore
    assert "files/etc/app/.zcompdump" in gitignore
    assert "files/etc/app/x.pyc" in gitignore
    assert "files/etc/app/__pycache__" in gitignore


def test_capture_directory_ignore_vendored_git(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    src_dir = tmp_path / "etc" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "keep.conf").write_text("keep")
    vendored = src_dir / ".antidote"
    vendored.mkdir()
    (vendored / "README.md").write_text("r")
    (vendored / ".git").mkdir()
    (vendored / ".git" / "HEAD").write_text("ref")

    assert (
        commands.capture(
            str(base_dir),
            [str(src_dir)],
            [".antidote"],
            False,
            False,
            root=str(tmp_path),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "skip (ignored)" in out
    # The directory is captured as a dir-link; the vendored clone moved along.
    tracked_dir = base_dir / "files" / "etc" / "app"
    assert os.path.islink(src_dir)
    assert os.path.realpath(src_dir) == os.path.realpath(tracked_dir)
    assert (tracked_dir / "keep.conf").read_text() == "keep"
    assert (tracked_dir / ".antidote" / ".git" / "HEAD").read_text() == "ref"
    # The vendored clone is gitignored.
    gitignore = (base_dir / ".gitignore").read_text()
    assert "files/etc/app/.antidote" in gitignore


def test_deploy_conflict_requires_force(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    target.write_text("other")
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")

    assert commands.deploy(str(base_dir), [], False, False) == 1
    assert not os.path.islink(target)
    assert target.read_text() == "other"

    assert commands.deploy(str(base_dir), [], True, False) == 0
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)
    # The overwritten file is kept as a .bak backup.
    backup = tmp_path / "foo.conf.bak"
    assert backup.read_text() == "other"


def test_deploy_force_fixes_misplaced_symlink(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("correct")
    other = base_dir / "files" / "etc" / "other.conf"
    other.write_text("other")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    # A managed symlink pointing at the WRONG tracked file.
    target.symlink_to(other)

    # Without --force: refused, symlink left untouched.
    assert (
        commands.deploy(str(base_dir), ["etc/app.conf"], False, False, root=str(root))
        == 1
    )
    assert os.path.realpath(target) == os.path.realpath(other)
    # With --force: re-linked to the correct tracked file.
    assert (
        commands.deploy(str(base_dir), ["etc/app.conf"], True, False, root=str(root))
        == 0
    )
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_deploy_retries_with_sudo_on_permission_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("x")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)

    sudo_calls: list[list[str]] = []
    monkeypatch.setattr(commands, "_sudo", sudo_calls.append)

    def fake_makedirs(*args: object, **kwargs: object) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(commands.os, "makedirs", fake_makedirs)

    assert (
        commands.deploy(str(base_dir), ["etc/app.conf"], False, False, root=str(root))
        == 0
    )
    # The permission error triggered a sudo fallback for the mkdir.
    assert any(cmd == ["mkdir", "-p", str(root / "etc")] for cmd in sudo_calls)


def test_deploy_idempotent(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")

    assert commands.deploy(str(base_dir), [], False, False) == 0
    rel = os.path.relpath(os.fspath(target), "/")
    # Links point directly at the tracked file (no indirection).
    assert os.readlink(target) == os.path.join(str(base_dir), "files", rel)
    assert commands.deploy(str(base_dir), [], False, False) == 0
    assert os.path.islink(target)


def test_deploy_specific_untracked_path_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    # The requested path has no tracked file: deploy reports it instead of
    # silently doing nothing.
    assert (
        commands.deploy(
            str(base_dir), ["home/cbenz/.config/Thunar/uca.xml"], False, False
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "not a tracked file" in out


def test_deploy_leaves_valid_dir_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "etc" / "foo" / "bar.txt"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("content")

    foo_link = tmp_path / "etc" / "foo"
    foo_link.parent.mkdir(parents=True, exist_ok=True)
    # The dir-link points at the tracked file's parent directory.
    foo_link.symlink_to(tracked.parent, target_is_directory=True)

    assert commands.deploy(str(base_dir), [], False, False) == 0
    out = capsys.readouterr().out
    # A valid managed dir-link is the deployed state: it is left alone and the
    # file is served through it (no individual symlink is created).
    assert os.path.islink(foo_link)
    assert os.path.realpath(foo_link) == os.path.realpath(tracked.parent)
    assert not os.path.islink(target)
    assert "linked via dir-link" in out
    assert target.read_text() == "content"


def test_deploy_repairs_dangling_dir_link(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "foo" / "bar.txt"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    foo_link = root / "etc" / "foo"
    foo_link.parent.mkdir(parents=True, exist_ok=True)
    # A managed dir-link whose base-dir location is missing (e.g. repo moved).
    foo_link.symlink_to(
        base_dir / "files" / "etc" / "missing", target_is_directory=True
    )

    assert commands.deploy(str(base_dir), [], False, False, root=str(root)) == 0
    # The dangling dir-link is replaced by a real directory + individual link.
    assert not os.path.islink(foo_link)
    assert os.path.isdir(foo_link)
    assert os.path.islink(foo_link / "bar.txt")
    assert os.path.realpath(foo_link / "bar.txt") == os.path.realpath(tracked)


def test_dry_run_deploy_makes_no_changes(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"  # does not exist yet
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")

    assert commands.deploy(str(base_dir), [], False, True) == 0
    assert not os.path.lexists(target)


def test_dry_run_capture_makes_no_changes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "keep.txt"
    source.write_text("hello")

    assert commands.capture(str(base_dir), [str(source)], [], False, True) == 0
    assert os.path.isfile(source) and not os.path.islink(source)
    assert not place_tracked(base_dir, source).exists()


def test_dry_run_notice_is_first_line(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "keep.txt"
    source.write_text("hello")

    assert commands.capture(str(base_dir), [str(source)], [], False, True) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "dry-run: preview only, nothing will be changed"


def test_status_detects_drift(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")
    target.write_text("different")

    assert commands.status(str(base_dir)) == 1


def test_ls_lists_tracked_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "files" / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc" / "fstab").write_text("x")
    (base_dir / "files" / "home" / "cbenz").mkdir(parents=True)
    (base_dir / "files" / "home" / "cbenz" / ".bashrc").write_text("y")

    assert commands.ls(str(base_dir)) == 0
    out = capsys.readouterr().out
    # Target locations, with a leading slash.
    assert out.splitlines() == ["/etc/fstab", "/home/cbenz/.bashrc"]


def test_ls_ignores_gitignored_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "files" / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc" / "fstab").write_text("x")
    zsh = base_dir / "files" / "home" / "cbenz" / ".config" / "zsh"
    zsh.mkdir(parents=True)
    (zsh / ".zshrc").write_text("r")
    (zsh / ".zsh_history").write_text("h")
    # The repository .gitignore excludes the transient files (as `myfiles
    # ignore` writes them): they are not listed.
    (base_dir / ".gitignore").write_text(
        "# managed by myfiles\nfiles/home/cbenz/.config/zsh/.zsh_history\n"
    )

    assert commands.ls(str(base_dir)) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["/etc/fstab", "/home/cbenz/.config/zsh/.zshrc"]


def test_ls_empty_lists_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.ls(str(base_dir)) == 0
    out = capsys.readouterr().out
    assert out == ""


def test_deploy_confirmation_no_aborts(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "_ask_confirmation", _confirm_no)
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")

    assert commands.deploy(str(base_dir), [], False, False) == 0
    assert not os.path.lexists(target)


def test_links_point_directly_at_tracked_file(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("content")

    assert commands.deploy(str(base_dir), [], False, False) == 0
    rel = os.path.relpath(os.fspath(target), "/")
    # Links point directly at the tracked file — no indirection involved.
    assert os.readlink(target) == os.path.join(str(base_dir), "files", rel)
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_commands_error_without_base_dir(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # Not run from a repository (no files/ directory in the CWD ancestors) and
    # no MYFILES_BASE_DIR set.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MYFILES_BASE_DIR", raising=False)
    assert commands.status(None) == 1
    assert commands.deploy(None, [], False, False) == 1


def test_commands_resolve_from_repo_cwd(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    place_tracked(base_dir, target).write_text("tracked")

    # Run from inside the repository: the base-dir is discovered via files/.
    monkeypatch.chdir(base_dir)
    assert commands.deploy(None, [], False, False) == 0
    assert os.path.islink(target)


def test_commands_resolve_from_env_var(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    place_tracked(base_dir, target).write_text("tracked")

    # Run from anywhere (not the repo): MYFILES_BASE_DIR provides the root.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MYFILES_BASE_DIR", str(base_dir))
    assert commands.deploy(None, [], False, False) == 0
    assert os.path.islink(target)
    assert commands.status(None) == 0


def test_commands_base_dir_option_beats_env_var(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    target = tmp_path / "foo.conf"
    place_tracked(base_dir, target).write_text("tracked")

    # The env var points elsewhere; the explicit --base-dir wins.
    monkeypatch.setenv("MYFILES_BASE_DIR", str(other))
    assert commands.deploy(str(base_dir), [], False, False) == 0
    assert os.path.islink(target)


def test_deploy_with_alternative_root(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    tracked = base_dir / "files" / "etc" / "fstab"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("rootfs content")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    assert commands.deploy(str(base_dir), [], False, False, root=str(sandbox)) == 0
    target = sandbox / "etc" / "fstab"
    assert os.path.islink(target)
    assert target.read_text() == "rootfs content"
    assert not os.path.lexists(tmp_path / "etc" / "fstab")


def test_capture_with_alternative_root(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    sandbox = tmp_path / "sandbox"
    source = sandbox / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("hello")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], False, False, root=str(sandbox)
        )
        == 0
    )
    assert (base_dir / "files" / "etc" / "app.conf").read_text() == "hello"
    assert os.path.islink(source)


def test_diff_by_target_path(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    target = root / "etc" / "UPower" / "UPower.conf"
    target.parent.mkdir(parents=True)
    target.write_text("target content")
    tracked = base_dir / "files" / "etc" / "UPower" / "UPower.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked content")

    assert commands.diff(str(base_dir), str(target), root=str(root)) == 1


def test_diff_identical(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("same")
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("same")

    assert commands.diff(str(base_dir), str(target), root=str(root)) == 0


def test_diff_by_tracked_path(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("repo content")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system content")

    assert commands.diff(str(base_dir), str(tracked), root=str(root)) == 1


def test_diff_order_tracked_is_before_target(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system\n")
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("repo\n")

    assert commands.diff(str(base_dir), str(target), root=str(root)) == 1
    out = capfd.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("--- ") and str(tracked) in lines[0]
    assert any(line.startswith("+++ ") and str(target) in line for line in lines)


def test_status_valid_dir_link_is_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "home" / "cbenz" / ".config" / "waybar" / "style.css"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    dir_link = root / "home" / "cbenz" / ".config" / "waybar"
    dir_link.parent.mkdir(parents=True)
    dir_link.symlink_to(tracked.parent, target_is_directory=True)

    # A valid managed dir-link is healthy: status reports nothing.
    assert commands.status(str(base_dir), root=str(root)) == 0
    out = capsys.readouterr().out
    assert out == ""


def test_status_valid_dir_link_multiple_files_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    waybar = base_dir / "files" / "home" / "cbenz" / ".config" / "waybar"
    waybar.mkdir(parents=True)
    for name in ["style.css", "config.jsonc", "mic.sh"]:
        (waybar / name).write_text(name)
    dir_link = root / "home" / "cbenz" / ".config" / "waybar"
    dir_link.parent.mkdir(parents=True)
    dir_link.symlink_to(waybar, target_is_directory=True)

    # Every file under a valid dir-link is healthy: nothing is reported.
    assert commands.status(str(base_dir), root=str(root)) == 0
    out = capsys.readouterr().out
    assert out == ""


def test_status_flags_dangling_foreign_symlink(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "nsswitch.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "nsswitch.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "does-not-exist.conf")

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    assert "dangling" in out
    assert "->" in out
    assert "does-not-exist.conf" in out


def test_status_flags_foreign_dir_symlink_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    # Tracked files under home/cbenz/.ssh/config.d/...
    config_dir = base_dir / "files" / "home" / "cbenz" / ".ssh" / "config.d"
    config_dir.mkdir(parents=True)
    for name in ["dbnomics", "home", "openfisca"]:
        (config_dir / name).write_text(name)
    # ~/.ssh/config.d is a FOREIGN directory symlink (points outside base-dir).
    foreign_dir = tmp_path / "other" / "ssh" / "config.d"
    foreign_dir.mkdir(parents=True)
    config_d = root / "home" / "cbenz" / ".ssh" / "config.d"
    config_d.parent.mkdir(parents=True)
    config_d.symlink_to(foreign_dir, target_is_directory=True)

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    # Reported once as the foreign directory symlink, not per-file missing;
    # a final hint suggests running `fix`.
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("foreign ")
    assert "->" in lines[0]
    assert "config.d" in lines[0]
    assert "missing" not in out
    assert lines[1] == ""
    assert "myfiles fix" in lines[2]


def test_status_flags_elsewhere_with_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("expected")
    other = base_dir / "files" / "etc" / "other.conf"
    other.write_text("other")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    # A managed symlink, but pointing at the wrong tracked file.
    target.symlink_to(other)

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    assert "elsewhere" in out
    assert "->" in out
    assert "other.conf" in out


def test_status_specific_target_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tracked)

    assert commands.status(str(base_dir), [str(target)], root=str(root)) == 0
    out = capsys.readouterr().out
    assert out == ""  # healthy managed symlinks are hidden


def test_status_specific_not_tracked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    target = root / "etc" / "other.conf"
    target.parent.mkdir(parents=True)
    target.write_text("content")

    assert commands.status(str(base_dir), [str(target)], root=str(root)) == 0
    out = capsys.readouterr().out
    assert out == f"not tracked {target}\n"


def test_status_specific_tracked_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tracked)

    assert commands.status(str(base_dir), [str(tracked)], root=str(root)) == 0
    out = capsys.readouterr().out
    assert out == ""  # healthy managed symlinks are hidden


def test_status_specific_multiple_only_reports_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_a = base_dir / "files" / "etc" / "a.conf"
    tracked_a.parent.mkdir(parents=True)
    tracked_a.write_text("a")
    target_a = root / "etc" / "a.conf"
    target_a.parent.mkdir(parents=True)
    target_a.symlink_to(tracked_a)
    tracked_b = base_dir / "files" / "etc" / "b.conf"
    tracked_b.write_text("b")  # not deployed (no target yet)

    assert commands.status(str(base_dir), [str(target_a)], root=str(root)) == 0
    out = capsys.readouterr().out
    # Only the requested path is considered; it is healthy, so nothing is shown.
    assert out == ""


def test_capture_forbids_inside_base_dir(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = base_dir / "files" / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("content")

    assert commands.capture(str(base_dir), [str(source)], [], False, False) == 1
    assert source.read_text() == "content"


def test_capture_force_overwrites_tracked_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old")
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("new")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], True, False, root=str(tmp_path)
        )
        == 0
    )
    assert tracked.read_text() == "new"
    assert os.path.islink(source)
    # Git is the safety net for capture: no .bak is created.
    assert not (base_dir / "files" / "etc" / "app.conf.bak").exists()


def test_capture_force_refuses_dirty_tracked_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    subprocess.run(["git", "-C", str(base_dir), "init", "-q"], check=False)
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.email", "t@t"], check=False
    )
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.name", "T"], check=False
    )
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("committed")
    subprocess.run(["git", "-C", str(base_dir), "add", "-A"], check=False)
    subprocess.run(["git", "-C", str(base_dir), "commit", "-qm", "init"], check=False)
    # Make the tracked file dirty (uncommitted modification).
    tracked.write_text("dirty")
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("new")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], True, False, root=str(tmp_path)
        )
        == 1
    )
    # The dirty tracked file is left untouched, so no version is lost.
    assert tracked.read_text() == "dirty"
    assert source.read_text() == "new"


def test_capture_does_not_commit_to_git_repo(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    subprocess.run(["git", "-C", str(base_dir), "init", "-q"], check=False)
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.email", "test@example.com"],
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.name", "Test"],
        check=False,
    )
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("new")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], False, False, root=str(tmp_path)
        )
        == 0
    )
    # myfiles never creates commits: the captured file stays untracked and no
    # commit message is printed.
    out = capsys.readouterr().out
    assert "committed:" not in out
    status = subprocess.run(
        ["git", "-C", str(base_dir), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    # The captured file is untracked (git collapses the dir to `etc/`).
    assert "files/" in status.stdout


def test_capture_cancelled_does_not_commit(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "_ask_confirmation", _confirm_no)
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    subprocess.run(["git", "-C", str(base_dir), "init", "-q"], check=False)
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.email", "test@example.com"],
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(base_dir), "config", "user.name", "Test"],
        check=False,
    )
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("new")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], False, False, root=str(tmp_path)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "cancelled" in out
    # No commit is attempted after a cancelled confirmation: no warning.
    assert "warning" not in out
    # The source is left untouched (still a real file, not moved or linked).
    source_path = tmp_path / "etc" / "app.conf"
    assert os.path.isfile(source_path)
    assert not os.path.islink(source_path)


def test_capture_managed_symlink_is_skipped(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("hello")

    assert commands.capture(str(base_dir), [str(source)], [], False, False) == 0
    # The source is now a managed symlink into base-dir: re-capturing is a no-op.
    assert commands.capture(str(base_dir), [str(source)], [], False, False) == 0
    assert os.path.islink(source)


def test_capture_reports_already_captured_symlink(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("hello")

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], False, False, root=str(tmp_path)
        )
        == 0
    )
    capsys.readouterr()  # clear the first capture's output

    assert (
        commands.capture(
            str(base_dir), [str(source)], [], False, False, root=str(tmp_path)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "skip (already captured)" in out
    assert "nothing to capture" not in out


def test_eject_restores_file_when_link_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    # The managed link was deleted: nothing exists at the target.
    target = root / "etc" / "app.conf"

    assert commands.eject(str(base_dir), ["etc/app.conf"], False, root=str(root)) == 0
    out = capsys.readouterr().out
    # The tracked content is restored at the target (the missing link is not
    # recreated), with a message showing the real source and destination.
    assert f"move {tracked} to {target}" in out
    assert "not a managed symlink" not in out
    assert os.path.isfile(target) and not os.path.islink(target)
    assert target.read_text() == "content"
    # The tracked copy is removed and the now-empty directories pruned.
    assert not tracked.exists()
    assert not (base_dir / "files" / "etc").exists()


def test_eject_tracked_absolute_path_maps_to_target(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tracked)

    # Ejecting by the absolute tracked path (inside base-dir) is equivalent to
    # ejecting by the system target path.
    assert commands.eject(str(base_dir), [str(tracked)], False, root=str(root)) == 0
    assert os.path.isfile(target) and not os.path.islink(target)
    assert target.read_text() == "content"
    assert not tracked.exists()


def test_capture_directory_in_sandbox_root(tmp_path: Path) -> None:
    # ALLOWED_DIR_ROOTS is NOT patched: --root-dir maps /etc under the sandbox,
    # so the directory capture is allowed.
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    sandbox = tmp_path / "sandbox"
    src_dir = sandbox / "etc" / "app"
    src_dir.mkdir(parents=True)
    (src_dir / "keep.conf").write_text("keep")

    assert (
        commands.capture(
            str(base_dir), [str(src_dir)], [], False, False, root=str(sandbox)
        )
        == 0
    )
    # The whole directory was moved into base-dir and replaced by a dir-link.
    assert (base_dir / "files" / "etc" / "app" / "keep.conf").read_text() == "keep"
    assert os.path.islink(src_dir)
    assert os.path.realpath(src_dir) == os.path.realpath(
        base_dir / "files" / "etc" / "app"
    )
    assert (src_dir / "keep.conf").read_text() == "keep"
    assert not os.path.islink(src_dir / "keep.conf")


def test_capture_identical_tracked_shows_only_link(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    source = root / "etc" / "app.conf"
    source.parent.mkdir(parents=True)
    source.write_text("same")
    # Already tracked with identical content: nothing to move, only to link.
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("same")

    assert (
        commands.capture(str(base_dir), [str(source)], [], False, False, root=str(root))
        == 0
    )
    out = capsys.readouterr().out
    assert "skip (identical)" not in out
    assert "link" in out
    assert os.path.islink(source)
    assert os.path.realpath(source) == os.path.realpath(tracked)


def test_capture_dir_link_idempotent_skip(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "Thunar"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "accels.scm").write_text("a")
    dir_link = root / "home" / "cbenz" / ".config" / "Thunar"
    dir_link.parent.mkdir(parents=True)
    dir_link.symlink_to(tracked_dir, target_is_directory=True)

    assert (
        commands.capture(
            str(base_dir), [str(dir_link)], [], False, False, root=str(root)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "skip (already captured)" in out
    # The dir-link is left untouched.
    assert os.path.islink(dir_link)
    assert os.path.realpath(dir_link) == os.path.realpath(tracked_dir)


def test_capture_directory_converts_fully_managed_to_dir_link(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    # Files already captured individually (the Thunar case).
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "Thunar"
    tracked_dir.mkdir(parents=True)
    for name in ["accels.scm", "renamerrc"]:
        (tracked_dir / name).write_text(name)
    real_dir = root / "home" / "cbenz" / ".config" / "Thunar"
    real_dir.mkdir(parents=True)
    # Fully managed: only correctly-linked managed symlinks, zero drift.
    (real_dir / "accels.scm").symlink_to(tracked_dir / "accels.scm")
    (real_dir / "renamerrc").symlink_to(tracked_dir / "renamerrc")

    assert (
        commands.capture(
            str(base_dir), [str(real_dir)], [], False, False, root=str(root)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "convert to dir-link" in out
    # The real directory was removed and replaced by a dir-link into base-dir.
    assert os.path.islink(real_dir)
    assert os.path.realpath(real_dir) == os.path.realpath(tracked_dir)
    # The tracked files are untouched (nothing was moved).
    assert (tracked_dir / "accels.scm").read_text() == "accels.scm"
    assert (tracked_dir / "renamerrc").read_text() == "renamerrc"


def test_capture_directory_refuses_drift(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "Thunar"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "accels.scm").write_text("old")
    real_dir = root / "home" / "cbenz" / ".config" / "Thunar"
    real_dir.mkdir(parents=True)
    (real_dir / "accels.scm").write_text("new")  # real file: drift

    assert (
        commands.capture(
            str(base_dir), [str(real_dir)], [], False, False, root=str(root)
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "not fully managed" in out
    assert "drift" in out
    # Nothing changed: still a real dir, tracked file untouched.
    assert not os.path.islink(real_dir)
    assert (tracked_dir / "accels.scm").read_text() == "old"
    assert (real_dir / "accels.scm").read_text() == "new"


def test_capture_directory_accepts_identical_real_file(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "git"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "gitk").write_text("content")
    (tracked_dir / "ignore").write_text("ignore")
    real_dir = root / "home" / "cbenz" / ".config" / "git"
    real_dir.mkdir(parents=True)
    # gitk is a real file but identical to its tracked copy (no diff); ignore
    # is a managed symlink. The directory is convertible to a dir-link.
    (real_dir / "gitk").write_text("content")
    (real_dir / "ignore").symlink_to(tracked_dir / "ignore")

    assert (
        commands.capture(
            str(base_dir), [str(real_dir)], [], False, False, root=str(root)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "convert to dir-link" in out
    # The real directory is replaced by a dir-link; the tracked copies are
    # untouched and served through it.
    assert os.path.islink(real_dir)
    assert os.path.realpath(real_dir) == os.path.realpath(tracked_dir)
    assert (real_dir / "gitk").read_text() == "content"
    assert (tracked_dir / "gitk").read_text() == "content"
    assert (tracked_dir / "ignore").read_text() == "ignore"


def test_capture_fully_managed_dir_with_ignore_converts_to_dir_link(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The `~/.config/zsh` case: files already captured individually, plus
    # transient real files passed with `--ignore`. The directory is collapsed
    # into a dir-link and the ignored entries are gitignored.
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "zsh"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / ".zshrc").write_text("zshrc")
    real_dir = root / "home" / "cbenz" / ".config" / "zsh"
    real_dir.mkdir(parents=True)
    (real_dir / ".zshrc").symlink_to(tracked_dir / ".zshrc")
    (real_dir / ".zsh_history").write_text("history")
    (real_dir / ".zcompdump").write_text("comp")

    assert (
        commands.capture(
            str(base_dir),
            [str(real_dir)],
            [".zsh_history", ".zcompdump"],
            False,
            False,
            root=str(root),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "convert to dir-link" in out
    assert "skip (ignored)" in out
    # The directory is now a dir-link into base-dir.
    assert os.path.islink(real_dir)
    assert os.path.realpath(real_dir) == os.path.realpath(tracked_dir)
    # The ignored entries were moved into base-dir (not lost) and keep working
    # through the dir-link; the tracked copy is untouched.
    assert (tracked_dir / ".zsh_history").read_text() == "history"
    assert (tracked_dir / ".zcompdump").read_text() == "comp"
    assert (real_dir / ".zsh_history").read_text() == "history"
    assert (tracked_dir / ".zshrc").read_text() == "zshrc"
    # The ignored entries were appended to the repository's .gitignore.
    gitignore = (base_dir / ".gitignore").read_text()
    assert "files/home/cbenz/.config/zsh/.zsh_history" in gitignore
    assert "files/home/cbenz/.config/zsh/.zcompdump" in gitignore


def test_capture_ignore_does_not_clobber_managed_symlink(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "zsh"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / ".zshrc").write_text("zshrc")
    real_dir = root / "home" / "cbenz" / ".config" / "zsh"
    real_dir.mkdir(parents=True)
    (real_dir / ".zshrc").symlink_to(tracked_dir / ".zshrc")  # managed symlink

    # `--ignore` matching a managed symlink must NOT move/clobber the tracked
    # file: it is already managed, so the conversion leaves it untouched.
    assert (
        commands.capture(
            str(base_dir), [str(real_dir)], [".zshrc"], False, False, root=str(root)
        )
        == 0
    )
    assert os.path.islink(real_dir)
    assert os.path.realpath(real_dir) == os.path.realpath(tracked_dir)
    # The tracked file is intact and served through the dir-link.
    assert (tracked_dir / ".zshrc").read_text() == "zshrc"
    assert (real_dir / ".zshrc").read_text() == "zshrc"
    # No .gitignore entry was written for a managed symlink.
    assert not (base_dir / ".gitignore").exists()


def test_capture_directory_force_adopts_system(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "Thunar"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "accels.scm").write_text("old")
    real_dir = root / "home" / "cbenz" / ".config" / "Thunar"
    real_dir.mkdir(parents=True)
    (real_dir / "accels.scm").write_text("new")  # drift

    assert (
        commands.capture(
            str(base_dir), [str(real_dir)], [], True, False, root=str(root)
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "forced: system content wins" in out
    # The system content became the tracked copy; the dir is now a dir-link.
    assert (tracked_dir / "accels.scm").read_text() == "new"
    assert os.path.islink(real_dir)
    assert os.path.realpath(real_dir) == os.path.realpath(tracked_dir)


def test_eject_dir_link_restores_directory(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "Thunar"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "accels.scm").write_text("a")
    (tracked_dir / "renamerrc").write_text("r")
    dir_link = root / "home" / "cbenz" / ".config" / "Thunar"
    dir_link.parent.mkdir(parents=True)
    dir_link.symlink_to(tracked_dir, target_is_directory=True)

    assert commands.eject(str(base_dir), [], False, root=str(root)) == 0
    # The dir-link is gone, replaced by a real directory with real copies.
    assert not os.path.islink(dir_link)
    assert os.path.isdir(dir_link)
    assert (dir_link / "accels.scm").read_text() == "a"
    assert (dir_link / "renamerrc").read_text() == "r"
    assert not os.path.islink(dir_link / "accels.scm")
    # The tracked copies are removed (empty parents pruned too).
    assert not (tracked_dir / "accels.scm").exists()
    assert not tracked_dir.exists()


def test_ignore_writes_gitignore(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert (
        commands.ignore(str(base_dir), ["home/cbenz/.config/zsh/.antidote"], False) == 0
    )
    gitignore = base_dir / ".gitignore"
    assert gitignore.is_file()
    # The path is resolved to its tracked location under files/.
    text = gitignore.read_text()
    assert "files/home/cbenz/.config/zsh/.antidote" in text
    assert "managed by myfiles" in text


def test_ignore_appends_and_deduplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.ignore(str(base_dir), ["etc/.zsh_history"], False) == 0
    gitignore = base_dir / ".gitignore"
    # Adding the same path again reports nothing to ignore.
    assert commands.ignore(str(base_dir), ["etc/.zsh_history"], False) == 0
    out = capsys.readouterr().out
    assert "nothing to ignore" in out
    assert gitignore.read_text().count("files/etc/.zsh_history") == 1
    # A second distinct path is appended, keeping the existing entries.
    assert commands.ignore(str(base_dir), ["etc/first.conf"], False) == 0
    text = gitignore.read_text()
    assert text.count("files/etc/.zsh_history") == 1
    assert "files/etc/first.conf" in text


def test_ignore_dry_run_writes_nothing(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.ignore(str(base_dir), ["etc/.zsh_history"], True) == 0
    assert not (base_dir / ".gitignore").exists()


def test_deploy_target_is_directory_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.mkdir(parents=True)  # a directory where the symlink should go

    assert (
        commands.deploy(str(base_dir), ["etc/app.conf"], False, False, root=str(root))
        == 1
    )
    out = capsys.readouterr().out
    assert "is a directory" in out
    assert os.path.isdir(target)
    assert not os.path.islink(target)


def test_deploy_nothing_to_deploy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.deploy(str(base_dir), [], False, False) == 0
    out = capsys.readouterr().out
    assert "nothing to deploy" in out


def test_eject_nothing_to_eject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.eject(str(base_dir), [], False) == 0
    out = capsys.readouterr().out
    assert "nothing to eject" in out


def test_eject_non_symlink_target_left_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("edited by user")  # no longer a symlink

    assert commands.eject(str(base_dir), ["etc/app.conf"], False, root=str(root)) == 0
    out = capsys.readouterr().out
    assert "not a managed symlink" in out
    # Both the target and the tracked copy are left untouched.
    assert target.read_text() == "edited by user"
    assert tracked.read_text() == "tracked"


def test_status_directory_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.mkdir(parents=True)  # a directory instead of a symlink

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    assert "directory" in out


def test_status_not_linked_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("same")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("same")  # identical content, not yet linked

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    assert "not-linked" in out


def test_status_missing_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    # The target does not exist.

    assert commands.status(str(base_dir), root=str(root)) == 1
    out = capsys.readouterr().out
    assert "missing" in out


def test_status_no_tracked_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert commands.status(str(base_dir)) == 0
    out = capsys.readouterr().out
    assert "no tracked files" in out


def test_capture_directory_outside_allowed_roots_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    src_dir = tmp_path / "somewhere" / "app"  # not under /etc, /usr, ~/.config, ...
    src_dir.mkdir(parents=True)
    (src_dir / "x").write_text("x")

    assert commands.capture(str(base_dir), [str(src_dir)], [], False, False) == 1
    out = capsys.readouterr().out
    assert "outside the allowed roots" in out


def test_capture_missing_path_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    missing = tmp_path / "does-not-exist"

    assert commands.capture(str(base_dir), [str(missing)], [], False, False) == 1
    out = capsys.readouterr().out
    assert "does not exist" in out


def test_capture_root_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    # Capturing the filesystem root itself is rejected (no filesystem change).
    assert commands.capture(str(base_dir), ["/"], [], False, False) == 1
    out = capsys.readouterr().out
    assert "filesystem root" in out


def test_capture_single_file_unaffected_by_ignore(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "keep.log"
    source.write_text("hello")

    # `--ignore` only affects directory captures: the single file is captured.
    assert commands.capture(str(base_dir), [str(source)], ["*.log"], False, False) == 0
    assert os.path.islink(source)
    assert place_tracked(base_dir, source).read_text() == "hello"


def test_diff_missing_target_reports_difference(tmp_path: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")

    # diff -Naur treats a missing target as empty -> differences.
    assert (
        commands.diff(str(base_dir), str(root / "etc" / "app.conf"), root=str(root))
        == 1
    )


def test_ls_skips_git_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "files" / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc" / "fstab").write_text("x")
    (base_dir / "files" / "etc" / ".git").mkdir()
    (base_dir / "files" / "etc" / ".git" / "HEAD").write_text("ref")

    # A vendored `.git` directory is not tracked: it is not listed.
    assert commands.ls(str(base_dir)) == 0
    assert capsys.readouterr().out.splitlines() == ["/etc/fstab"]


def test_cli_dispatch_capture(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from myfiles.cli import main

    monkeypatch.setattr(commands, "ALLOWED_DIR_ROOTS", (str(tmp_path),))
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    source = tmp_path / "keep.txt"
    source.write_text("hello")

    assert main(["capture", str(source), "-d", str(base_dir)]) == 0
    assert os.path.islink(source)


def test_cli_dispatch_deploy(tmp_path: Path) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    place_tracked(base_dir, target).write_text("content")

    assert main(["deploy", str(target), "-d", str(base_dir)]) == 0
    assert os.path.islink(target)


def test_cli_dispatch_eject(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert main(["eject", "-d", str(base_dir)]) == 0
    assert "nothing to eject" in capsys.readouterr().out


def test_cli_dispatch_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert main(["status", "-d", str(base_dir)]) == 0
    assert "no tracked files" in capsys.readouterr().out


def test_cli_dispatch_ignore(tmp_path: Path) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()

    assert main(["ignore", "etc/fstab", "-d", str(base_dir)]) == 0
    assert "files/etc/fstab" in (base_dir / ".gitignore").read_text()


def test_cli_dispatch_diff(tmp_path: Path) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system")

    assert (
        main(["diff", str(target), "-d", str(base_dir), "--root-dir", str(root)]) == 1
    )


def test_cli_dispatch_ls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    (base_dir / "files" / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc" / "fstab").write_text("x")

    assert main(["ls", "-d", str(base_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == ["/etc/fstab"]
