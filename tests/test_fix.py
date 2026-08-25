"""Tests for the interactive ``fix`` command and CLI argument requirements."""

import os
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from myfiles import commands


def _confirm_yes(*args: object, **kwargs: object) -> bool:
    return True


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


def _fixed_choice(choice: str):
    """Return a fake ``_ask_fix`` that always picks ``choice``."""

    def fake(label: str, target: str, allowed: list[str], default: str | None) -> str:
        return choice

    return fake


def _scripted_input(answers: list[str]):
    """Return a fake ``input`` that yields the given answers in order."""

    iterator = iter(answers)

    def fake(prompt: str) -> str:
        return next(iterator)

    return fake


def _scripted_confirmation(answers: list[bool]):
    """Return a fake ``_ask_confirmation`` that yields the given answers in order."""

    iterator = iter(answers)

    def fake(
        prompt: str = "Apply these changes? [Y/n] ", abort_on_interrupt: bool = False
    ) -> bool:
        return next(iterator)

    return fake


def _fail_if_called(*args: object, **kwargs: object) -> object:
    """Fail the test if invoked (used to assert a non-interactive mode)."""
    raise AssertionError("should not have prompted")


def test_fix_no_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    target = tmp_path / "foo.conf"
    tracked = place_tracked(base_dir, target)
    tracked.write_text("tracked")
    commands.deploy(str(base_dir), [], False, False)

    assert commands.fix(str(base_dir), False) == 0
    out = capsys.readouterr().out
    assert "no problems to fix" in out


def test_fix_dangling_deploys(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "nsswitch.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "nsswitch.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "does-not-exist.conf")
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_fix_drift_captures(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked-old")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system-new")  # drift: exists and differs
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("capture"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    # The system file became the tracked copy, linked back in place.
    assert tracked.read_text() == "system-new"
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_fix_drift_deploys_tracked(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system")  # drift: exists and differs
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    # The tracked file is the authority: the drifted system file is replaced
    # by the link, and the old system content is kept as a backup.
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)
    assert (root / "etc" / "app.conf.bak").read_text() == "system"


def test_fix_skip_leaves_untouched(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("system")  # drift
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("skip"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    assert target.read_text() == "system"
    assert not os.path.islink(target)


def test_fix_dry_run_makes_no_changes(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), True, root=str(root)) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "dry-run: preview only, nothing will be changed"
    assert os.path.islink(target)
    assert os.path.realpath(target) != os.path.realpath(tracked)


def test_fix_foreign_dir_symlink_deploys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    config_dir = base_dir / "files" / "home" / "cbenz" / ".ssh" / "config.d"
    config_dir.mkdir(parents=True)
    for name in ["dbnomics", "home"]:
        (config_dir / name).write_text(name)
    foreign_dir = tmp_path / "other" / "ssh" / "config.d"
    foreign_dir.mkdir(parents=True)
    config_d = root / "home" / "cbenz" / ".ssh" / "config.d"
    config_d.parent.mkdir(parents=True)
    config_d.symlink_to(foreign_dir, target_is_directory=True)
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    # The foreign directory symlink was replaced with a real directory and
    # every tracked file underneath is now linked.
    assert not os.path.islink(config_d)
    assert os.path.isdir(config_d)
    assert (config_d / "dbnomics").is_symlink()
    assert os.path.realpath(config_d / "dbnomics") == os.path.realpath(
        config_dir / "dbnomics"
    )
    assert os.path.realpath(config_d / "home") == os.path.realpath(config_dir / "home")


def test_fix_valid_dir_link_is_not_a_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked_dir = base_dir / "files" / "home" / "cbenz" / ".config" / "sway"
    tracked_dir.mkdir(parents=True)
    (tracked_dir / "config").write_text("sway")
    dir_link = root / "home" / "cbenz" / ".config" / "sway"
    dir_link.parent.mkdir(parents=True)
    dir_link.symlink_to(tracked_dir, target_is_directory=True)

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    out = capsys.readouterr().out
    assert "no problems to fix" in out


def test_fix_dangling_dir_link_deploys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "foo" / "bar.txt"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    foo_link = root / "etc" / "foo"
    foo_link.parent.mkdir(parents=True, exist_ok=True)
    # A managed dir-link whose base-dir location is missing (dangling).
    foo_link.symlink_to(
        base_dir / "files" / "etc" / "missing", target_is_directory=True
    )
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    # The dangling dir-link was replaced by a real directory + linked files.
    assert not os.path.islink(foo_link)
    assert os.path.isdir(foo_link)
    assert os.path.islink(foo_link / "bar.txt")
    assert os.path.realpath(foo_link / "bar.txt") == os.path.realpath(tracked)


def test_fix_drift_asks_until_valid_choice(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("new")  # drift, and no default for drift
    # Empty and invalid answers re-ask; "c" finally picks capture.
    monkeypatch.setattr("builtins.input", _scripted_input(["", "x", "c"]))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    assert tracked.read_text() == "new"
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_fix_ctrl_c_aborts_everything(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    # Two problems: the first one gets interrupted by Ctrl-C.
    targets: list[Path] = []
    (root / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc").mkdir(parents=True)
    for name in ["a.conf", "b.conf"]:
        tracked = base_dir / "files" / "etc" / name
        tracked.write_text("old")
        target = root / "etc" / name
        target.write_text("new")  # drift
        targets.append(target)
    calls: list[str] = []

    def raise_interrupt(prompt: str) -> str:
        calls.append(prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)

    assert commands.fix(str(base_dir), False, root=str(root)) == 130
    out = capsys.readouterr().out
    assert "aborted" in out
    # Interrupted on the first item: the second one was never prompted.
    assert len(calls) == 1
    # Nothing was captured or linked.
    for target in targets:
        assert not os.path.islink(target)
        assert target.read_text() == "new"


def test_fix_applies_each_item_as_it_goes(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc").mkdir(parents=True)
    tracked1 = base_dir / "files" / "etc" / "a.conf"
    tracked1.write_text("old-a")
    target1 = root / "etc" / "a.conf"
    target1.write_text("new-a")
    tracked2 = base_dir / "files" / "etc" / "b.conf"
    tracked2.write_text("old-b")
    target2 = root / "etc" / "b.conf"
    target2.write_text("new-b")
    calls: list[str] = []

    def scripted(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "c"  # capture item 1
        raise KeyboardInterrupt  # Ctrl-C on item 2

    monkeypatch.setattr("builtins.input", scripted)

    assert commands.fix(str(base_dir), False, root=str(root)) == 130
    # Item 1 was applied immediately, before the interruption on item 2.
    assert tracked1.read_text() == "new-a"
    assert os.path.islink(target1)
    assert os.path.realpath(target1) == os.path.realpath(tracked1)
    # Item 2 was never applied.
    assert tracked2.read_text() == "old-b"
    assert not os.path.islink(target2)
    assert target2.read_text() == "new-b"
    out = capsys.readouterr().out
    assert "aborted" in out
    assert "change(s) applied" in out


def test_fix_declined_confirmation_skips_item(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    (base_dir / "files" / "etc").mkdir(parents=True)
    tracked = base_dir / "files" / "etc" / "a.conf"
    tracked.write_text("repo")
    target = root / "etc" / "a.conf"  # missing: deploy would create the link
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))
    monkeypatch.setattr(commands, "_ask_confirmation", _scripted_confirmation([False]))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    out = capsys.readouterr().out
    assert "cancelled" in out
    # The declined confirmation left the item untouched.
    assert not os.path.islink(target)
    assert not os.path.exists(target)


def test_fix_paths_only_processes_selected(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc").mkdir(parents=True)
    tracked_a = base_dir / "files" / "etc" / "a.conf"
    tracked_a.write_text("old-a")
    target_a = root / "etc" / "a.conf"
    target_a.write_text("new-a")
    tracked_b = base_dir / "files" / "etc" / "b.conf"
    tracked_b.write_text("old-b")
    target_b = root / "etc" / "b.conf"
    target_b.write_text("new-b")
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("capture"))

    # Only a.conf is requested: b.conf is left untouched.
    assert commands.fix(str(base_dir), False, ["etc/a.conf"], root=str(root)) == 0
    assert tracked_a.read_text() == "new-a"
    assert os.path.islink(target_a)
    assert tracked_b.read_text() == "old-b"
    assert not os.path.islink(target_b)


def test_fix_defaults_applies_deploy_without_asking(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "nsswitch.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    target = root / "etc" / "nsswitch.conf"
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "does-not-exist.conf")  # dangling: default deploy
    # --defaults must not prompt: neither the choice nor the confirmation.
    monkeypatch.setattr(commands, "_ask_fix", _fail_if_called)
    monkeypatch.setattr(commands, "_ask_confirmation", _fail_if_called)

    assert commands.fix(str(base_dir), False, defaults=True, root=str(root)) == 0
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)


def test_fix_defaults_skips_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("new")  # drift: no default

    assert commands.fix(str(base_dir), False, defaults=True, root=str(root)) == 0
    out = capsys.readouterr().out
    assert "no default" in out
    # Untouched.
    assert target.read_text() == "new"
    assert not os.path.islink(target)
    assert tracked.read_text() == "old"


def test_fix_only_filters_problems(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc").mkdir(parents=True)
    tracked_a = base_dir / "files" / "etc" / "a.conf"
    tracked_a.write_text("old-a")
    target_a = root / "etc" / "a.conf"
    target_a.write_text("new-a")  # drift
    tracked_b = base_dir / "files" / "etc" / "b.conf"
    tracked_b.write_text("tracked-b")
    target_b = root / "etc" / "b.conf"
    target_b.symlink_to(tmp_path / "missing.conf")  # dangling
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    # Only the dangling problem is processed; the drift one is left untouched.
    assert commands.fix(str(base_dir), False, only=["dangling"], root=str(root)) == 0
    assert os.path.islink(target_b)
    assert os.path.realpath(target_b) == os.path.realpath(tracked_b)
    assert target_a.read_text() == "new-a"
    assert not os.path.islink(target_a)


def test_fix_only_repeatable(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (base_dir / "files" / "etc").mkdir(parents=True)
    tracked_a = base_dir / "files" / "etc" / "a.conf"
    tracked_a.write_text("old-a")
    target_a = root / "etc" / "a.conf"
    target_a.write_text("new-a")  # drift
    tracked_b = base_dir / "files" / "etc" / "b.conf"
    tracked_b.write_text("tracked-b")
    target_b = root / "etc" / "b.conf"  # missing: no target exists yet
    tracked_c = base_dir / "files" / "etc" / "c.conf"
    tracked_c.write_text("tracked-c")
    target_c = root / "etc" / "c.conf"
    target_c.symlink_to(tmp_path / "gone.conf")  # dangling
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    # Repeatable --only: missing and dangling are processed, drift is not.
    assert (
        commands.fix(str(base_dir), False, only=["missing", "dangling"], root=str(root))
        == 0
    )
    assert os.path.islink(target_b)
    assert os.path.realpath(target_b) == os.path.realpath(tracked_b)
    assert os.path.islink(target_c)
    assert os.path.realpath(target_c) == os.path.realpath(tracked_c)
    assert target_a.read_text() == "new-a"
    assert not os.path.islink(target_a)


def test_fix_only_unknown_type_errors_at_cli() -> None:
    from myfiles.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["fix", "--only", "bogus"])
    assert exc.value.code == 2


def test_deploy_requires_paths_at_cli() -> None:
    from myfiles.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["deploy"])
    assert exc.value.code == 2


def test_capture_requires_paths_at_cli() -> None:
    from myfiles.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["capture"])
    assert exc.value.code == 2


def test_cli_has_fix_command() -> None:
    from myfiles.cli import main

    # No base dir configured (and not run from a repository): fix errors.
    assert main(["fix"]) == 1


def test_fix_diff_choice_reasks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("new")  # drift
    # `i` (diff) shows the diff and re-asks; `d` then deploys (tracked wins).
    monkeypatch.setattr("builtins.input", _scripted_input(["i", "d"]))

    assert commands.fix(str(base_dir), False, root=str(root)) == 0
    assert os.path.islink(target)
    assert os.path.realpath(target) == os.path.realpath(tracked)
    assert (root / "etc" / "app.conf.bak").read_text() == "new"


def test_fix_defaults_skips_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("content")
    target = root / "etc" / "app.conf"
    target.mkdir(parents=True)  # directory: not auto-fixable

    assert commands.fix(str(base_dir), False, defaults=True, root=str(root)) == 0
    out = capsys.readouterr().out
    assert "not auto-fixable" in out


def test_fix_ctrl_c_during_confirmation(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    root = tmp_path / "root"
    tracked = base_dir / "files" / "etc" / "app.conf"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("old")
    target = root / "etc" / "app.conf"
    target.parent.mkdir(parents=True)
    target.write_text("new")  # drift
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    def raise_interrupt(
        prompt: str = "Apply these changes? [Y/n] ", abort_on_interrupt: bool = False
    ) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "_ask_confirmation", raise_interrupt)

    assert commands.fix(str(base_dir), False, root=str(root)) == 130
    out = capsys.readouterr().out
    assert "aborted" in out
    # Nothing was applied.
    assert not os.path.islink(target)
    assert target.read_text() == "new"
