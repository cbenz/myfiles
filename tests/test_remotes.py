"""Tests for the remote (SSH) feature: ``remotes/<host>`` mirrored by copy.

The SSH layer (``myfiles.remote``) is replaced by a fake local filesystem, so
no real SSH host is needed: a remote absolute path ``/rel`` on host ``<host>``
maps to ``<tmp>/fake-remote/<host>/<rel>``.
"""

import os
import shutil
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from myfiles import commands, remote
from myfiles.fs import hash_file


def _confirm_yes(*args: object, **kwargs: object) -> bool:
    return True


@pytest.fixture(autouse=True)
def confirm_yes(monkeypatch: MonkeyPatch) -> None:
    """Auto-accept confirmation prompts."""
    monkeypatch.setattr(commands, "_ask_confirmation", _confirm_yes)


@pytest.fixture()
def fake_hosts(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Replace the remote backend with a local fake host filesystem."""
    root = tmp_path / "fake-remote"
    root.mkdir()

    def mapped(host: str, rpath: str) -> Path:
        return root / host / rpath.lstrip("/")

    def fake_lexists(host: str, rpath: str) -> bool:
        return os.path.lexists(mapped(host, rpath))

    def fake_isdir(host: str, rpath: str) -> bool:
        return os.path.isdir(mapped(host, rpath))

    def fake_hash(host: str, rpath: str) -> str | None:
        path = mapped(host, rpath)
        if not os.path.isfile(path):
            return None
        return hash_file(str(path))

    def fake_mtime(host: str, rpath: str) -> float | None:
        path = mapped(host, rpath)
        if not os.path.lexists(path):
            return None
        return os.path.getmtime(path)

    def fake_walk(host: str, rpath: str) -> list[str]:
        base = mapped(host, rpath)
        if not os.path.isdir(base):
            return []
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                full = os.path.join(dirpath, name)
                sub = os.path.relpath(full, base)
                out.append(rpath.rstrip("/") + "/" + sub)
        return sorted(out)

    def fake_download(host: str, rpath: str, local_path: str) -> None:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        shutil.copyfile(mapped(host, rpath), local_path)

    def fake_upload(local_path: str, host: str, rpath: str) -> None:
        dst = mapped(host, rpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(local_path, dst)

    def fake_mkdirs(host: str, rpath: str) -> None:
        os.makedirs(mapped(host, rpath), exist_ok=True)

    monkeypatch.setattr(remote, "remote_lexists", fake_lexists)
    monkeypatch.setattr(remote, "remote_isdir", fake_isdir)
    monkeypatch.setattr(remote, "remote_hash", fake_hash)
    monkeypatch.setattr(remote, "remote_mtime", fake_mtime)
    monkeypatch.setattr(remote, "remote_walk", fake_walk)
    monkeypatch.setattr(remote, "remote_download", fake_download)
    monkeypatch.setattr(remote, "remote_upload", fake_upload)
    monkeypatch.setattr(remote, "remote_mkdirs", fake_mkdirs)
    return root


def host_file(fake_hosts: Path, host: str, rel: str, content: str) -> Path:
    """Write a file on the fake host at ``rel`` (relative to the remote root)."""
    path = fake_hosts / host / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def place_remote(base_dir: Path, host: str, rel: str, content: str) -> Path:
    """Write a tracked remote file under ``<repo>/remotes/<host>/<rel>``."""
    dest = base_dir / "remotes" / host / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return dest


def _fixed_choice(choice: str):
    """Return a fake ``_ask_fix`` that always picks ``choice``."""

    def fake(label: str, target: str, allowed: list[str], default: str | None) -> str:
        return choice

    return fake


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #


def test_parse_remote_arg() -> None:
    assert remote.parse_remote_arg("ender3:/home/admin/x") == (
        "ender3",
        "/home/admin/x",
    )
    assert remote.parse_remote_arg("/etc/fstab") is None  # local absolute path
    assert remote.parse_remote_arg("etc/fstab") is None  # local relative path
    assert remote.parse_remote_arg("foo:bar") is None  # relative remote path
    assert remote.parse_remote_arg("a/b:/x") is None  # slash in the host
    assert remote.parse_remote_arg(": /x") is None  # empty host


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #


def test_capture_remote_file(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    host_file(fake_hosts, "ender3", "home/admin/printer.cfg", "remote content")

    assert (
        commands.capture(
            str(base_dir), ["ender3:/home/admin/printer.cfg"], [], False, False
        )
        == 0
    )
    tracked = base_dir / "remotes" / "ender3" / "home" / "admin" / "printer.cfg"
    assert tracked.read_text() == "remote content"

    # Idempotent: capturing again skips (identical content).
    assert (
        commands.capture(
            str(base_dir), ["ender3:/home/admin/printer.cfg"], [], False, False
        )
        == 0
    )
    assert tracked.read_text() == "remote content"


def test_capture_remote_missing_reports_error(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.capture(str(base_dir), ["ender3:/nope.txt"], [], False, False) == 1


def test_capture_remote_directory(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    config = fake_hosts / "ender3" / "home" / "admin" / "printer_data" / "config"
    config.mkdir(parents=True)
    (config / "printer.cfg").write_text("p")
    (config / "moonraker.conf").write_text("m")
    (config / "templates").mkdir()
    (config / "templates" / "a.cfg").write_text("t")

    assert (
        commands.capture(
            str(base_dir), ["ender3:/home/admin/printer_data/config"], [], False, False
        )
        == 0
    )
    tracked = (
        base_dir / "remotes" / "ender3" / "home" / "admin" / "printer_data" / "config"
    )
    assert (tracked / "printer.cfg").read_text() == "p"
    assert (tracked / "moonraker.conf").read_text() == "m"
    assert (tracked / "templates" / "a.cfg").read_text() == "t"


def test_capture_remotes_scan_all(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    # A known tracked file whose remote copy differs: recaptured.
    tracked = place_remote(base_dir, "ender3", "home/admin/printer.cfg", "old")
    host_file(fake_hosts, "ender3", "home/admin/printer.cfg", "new")
    # A known tracked file identical on the remote: skipped.
    place_remote(base_dir, "ender3", "etc/motd", "same")
    host_file(fake_hosts, "ender3", "etc/motd", "same")

    assert commands.capture(str(base_dir), [], [], False, False, remotes=[]) == 0
    assert tracked.read_text() == "new"
    out = capsys.readouterr().out
    assert "copy ender3:/home/admin/printer.cfg" in out
    assert "skip (identical): ender3:/etc/motd" in out


def test_capture_remotes_scan_hosts(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "home/admin/printer.cfg", "old")
    place_remote(base_dir, "erroll", "etc/x.conf", "old")
    host_file(fake_hosts, "ender3", "home/admin/printer.cfg", "new1")
    host_file(fake_hosts, "erroll", "etc/x.conf", "new2")

    assert (
        commands.capture(str(base_dir), [], [], False, False, remotes=["ender3"]) == 0
    )
    assert (
        base_dir / "remotes" / "ender3" / "home" / "admin" / "printer.cfg"
    ).read_text() == "new1"
    # The unselected host is left untouched.
    assert (base_dir / "remotes" / "erroll" / "etc" / "x.conf").read_text() == "old"


def test_capture_remotes_unknown_host(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.capture(str(base_dir), [], [], False, False, remotes=["nope"]) == 1


def test_capture_remotes_dry_run_makes_no_changes(
    tmp_path: Path, fake_hosts: Path
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    tracked = place_remote(base_dir, "ender3", "home/admin/printer.cfg", "old")
    host_file(fake_hosts, "ender3", "home/admin/printer.cfg", "new")

    assert commands.capture(str(base_dir), [], [], False, True, remotes=[]) == 0
    assert tracked.read_text() == "old"


# --------------------------------------------------------------------------- #
# deploy
# --------------------------------------------------------------------------- #


def test_deploy_remote_file(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "home/admin/printer.cfg", "tracked")
    target = host_file(fake_hosts, "ender3", "home/admin/printer.cfg", "remote-old")

    assert (
        commands.deploy(str(base_dir), ["ender3:/home/admin/printer.cfg"], False, False)
        == 0
    )
    assert target.read_text() == "tracked"

    # Idempotent: identical content is not copied again.
    assert (
        commands.deploy(str(base_dir), ["ender3:/home/admin/printer.cfg"], False, False)
        == 0
    )
    assert target.read_text() == "tracked"


def test_deploy_remote_missing_target_creates(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/klipper.cfg", "content")

    assert (
        commands.deploy(str(base_dir), ["ender3:/etc/klipper.cfg"], False, False) == 0
    )
    assert (fake_hosts / "ender3" / "etc" / "klipper.cfg").read_text() == "content"


def test_deploy_remote_directory(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "home/admin/printer_data/config/printer.cfg", "p")
    place_remote(
        base_dir, "ender3", "home/admin/printer_data/config/templates/a.cfg", "t"
    )

    assert (
        commands.deploy(
            str(base_dir), ["ender3:/home/admin/printer_data/config"], False, False
        )
        == 0
    )
    assert (
        fake_hosts
        / "ender3"
        / "home"
        / "admin"
        / "printer_data"
        / "config"
        / "printer.cfg"
    ).read_text() == "p"
    assert (
        fake_hosts
        / "ender3"
        / "home"
        / "admin"
        / "printer_data"
        / "config"
        / "templates"
        / "a.cfg"
    ).read_text() == "t"


def test_deploy_remote_by_tracked_path(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    tracked = place_remote(base_dir, "ender3", "etc/motd", "hello")

    # A root-relative path under remotes/ selects a remote deploy.
    assert (
        commands.deploy(str(base_dir), ["remotes/ender3/etc/motd"], False, False) == 0
    )
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "hello"
    # The absolute tracked path works as well.
    assert commands.deploy(str(base_dir), [str(tracked)], False, False) == 0


def test_deploy_remote_not_tracked_reports(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert (
        commands.deploy(str(base_dir), ["ender3:/etc/unknown.conf"], False, False) == 0
    )
    out = capsys.readouterr().out
    assert "not a tracked file" in out


def test_deploy_remote_dry_run_makes_no_changes(
    tmp_path: Path, fake_hosts: Path
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "content")

    assert commands.deploy(str(base_dir), ["ender3:/etc/motd"], False, True) == 0
    assert not (fake_hosts / "ender3" / "etc" / "motd").exists()


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def test_diff_remote(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    target = host_file(fake_hosts, "ender3", "etc/motd", "remote-different")

    assert commands.diff(str(base_dir), "ender3:/etc/motd") == 1

    target.write_text("tracked")
    assert commands.diff(str(base_dir), "ender3:/etc/motd") == 0


def test_diff_remote_missing_reports_error(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.diff(str(base_dir), "ender3:/nope.txt") == 1
    assert "does not exist" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# status --remotes
# --------------------------------------------------------------------------- #


def test_status_remote_reports_drift(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    target = host_file(fake_hosts, "ender3", "etc/motd", "different")
    os.utime(target, (1_700_000_000, 1_700_000_000))
    # An identical file is hidden.
    place_remote(base_dir, "ender3", "etc/same", "same")
    host_file(fake_hosts, "ender3", "etc/same", "same")

    assert commands.status(str(base_dir), remotes=[]) == 1
    out = capsys.readouterr().out
    assert "drift ender3:/etc/motd" in out
    assert "local:" in out and "remote:" in out
    assert "(newer)" in out
    assert "etc/same" not in out


def test_status_remote_identical_is_clean(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "same")
    host_file(fake_hosts, "ender3", "etc/motd", "same")

    assert commands.status(str(base_dir), remotes=[]) == 0


def test_status_remote_missing(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "x")

    assert commands.status(str(base_dir), remotes=[]) == 1
    out = capsys.readouterr().out
    assert "missing ender3:/etc/motd" in out


# --------------------------------------------------------------------------- #
# fix --remotes
# --------------------------------------------------------------------------- #


def test_fix_remote_drift_deploys(
    tmp_path: Path, fake_hosts: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    target = host_file(fake_hosts, "ender3", "etc/motd", "remote")
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("deploy"))

    assert commands.fix(str(base_dir), False, remotes=[]) == 0
    assert target.read_text() == "tracked"


def test_fix_remote_drift_captures(
    tmp_path: Path, fake_hosts: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    tracked = place_remote(base_dir, "ender3", "etc/motd", "tracked")
    host_file(fake_hosts, "ender3", "etc/motd", "remote")
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("capture"))

    assert commands.fix(str(base_dir), False, remotes=[]) == 0
    assert tracked.read_text() == "remote"


def test_fix_remote_defaults_deploys_missing(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "x")

    assert commands.fix(str(base_dir), False, remotes=[], defaults=True) == 0
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "x"


def test_fix_remote_no_problems(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "same")
    host_file(fake_hosts, "ender3", "etc/motd", "same")

    assert commands.fix(str(base_dir), False, remotes=[]) == 0
    assert "no problems to fix" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# unified --remotes (0..N host names) and ls --remotes
# --------------------------------------------------------------------------- #


def test_ls_remotes_lists_remote_files(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "home/admin/printer.cfg", "p")
    place_remote(base_dir, "ender3", "etc/motd", "m")
    place_remote(base_dir, "erroll", "etc/x.conf", "x")

    assert commands.ls(str(base_dir), remotes=[]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "ender3:/etc/motd",
        "ender3:/home/admin/printer.cfg",
        "erroll:/etc/x.conf",
    ]


def test_ls_remotes_specific_hosts(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "m")
    place_remote(base_dir, "erroll", "etc/x.conf", "x")

    assert commands.ls(str(base_dir), remotes=["ender3"]) == 0
    assert capsys.readouterr().out.splitlines() == ["ender3:/etc/motd"]


def test_ls_remotes_unknown_host_errors(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.ls(str(base_dir), remotes=["nope"]) == 1
    assert "not a known remote" in capsys.readouterr().out


def test_ls_remotes_honors_gitignore(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "m")
    place_remote(base_dir, "ender3", "etc/tmp.log", "t")
    (base_dir / ".gitignore").write_text(
        "# managed by myfiles\nremotes/ender3/etc/tmp.log\n"
    )

    assert commands.ls(str(base_dir), remotes=[]) == 0
    assert capsys.readouterr().out.splitlines() == ["ender3:/etc/motd"]


def test_status_remotes_filters_hosts(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "old")
    host_file(fake_hosts, "ender3", "etc/motd", "new")
    place_remote(base_dir, "erroll", "etc/x.conf", "old")
    host_file(fake_hosts, "erroll", "etc/x.conf", "new")

    # Only ender3 is reported.
    assert commands.status(str(base_dir), remotes=["ender3"]) == 1
    out = capsys.readouterr().out
    assert "ender3:/etc/motd" in out
    assert "erroll" not in out


def test_status_remotes_unknown_host_errors(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.status(str(base_dir), remotes=["nope"]) == 1
    assert "not a known remote" in capsys.readouterr().out


def test_fix_remotes_filters_hosts(
    tmp_path: Path, fake_hosts: Path, monkeypatch: MonkeyPatch
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "old")
    host_file(fake_hosts, "ender3", "etc/motd", "new")
    place_remote(base_dir, "erroll", "etc/x.conf", "old")
    host_file(fake_hosts, "erroll", "etc/x.conf", "new")
    monkeypatch.setattr(commands, "_ask_fix", _fixed_choice("capture"))

    assert commands.fix(str(base_dir), False, remotes=["ender3"]) == 0
    # ender3 is recaptured, erroll is left untouched.
    assert (base_dir / "remotes" / "ender3" / "etc" / "motd").read_text() == "new"
    assert (base_dir / "remotes" / "erroll" / "etc" / "x.conf").read_text() == "old"


# --------------------------------------------------------------------------- #
# deploy --remotes (0..N host names)
# --------------------------------------------------------------------------- #


def test_deploy_remotes_scan_all(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    host_file(fake_hosts, "ender3", "etc/motd", "remote-old")
    # An identical file is skipped.
    place_remote(base_dir, "erroll", "etc/x.conf", "x")
    host_file(fake_hosts, "erroll", "etc/x.conf", "x")

    assert commands.deploy(str(base_dir), [], False, False, remotes=[]) == 0
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "tracked"
    out = capsys.readouterr().out
    assert "copy" in out
    assert "skip (identical): erroll:/etc/x.conf" in out


def test_deploy_remotes_scan_hosts(tmp_path: Path, fake_hosts: Path) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "old")
    place_remote(base_dir, "erroll", "etc/x.conf", "old")
    host_file(fake_hosts, "ender3", "etc/motd", "remote1")
    host_file(fake_hosts, "erroll", "etc/x.conf", "remote2")

    assert commands.deploy(str(base_dir), [], False, False, remotes=["ender3"]) == 0
    # Only ender3 is deployed; erroll is left untouched.
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "old"
    assert (fake_hosts / "erroll" / "etc" / "x.conf").read_text() == "remote2"


def test_deploy_remotes_unknown_host_errors(
    tmp_path: Path, fake_hosts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    assert commands.deploy(str(base_dir), [], False, False, remotes=["nope"]) == 1
    assert "not a known remote" in capsys.readouterr().out


def test_deploy_remotes_dry_run_makes_no_changes(
    tmp_path: Path, fake_hosts: Path
) -> None:
    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    host_file(fake_hosts, "ender3", "etc/motd", "remote-old")

    assert commands.deploy(str(base_dir), [], False, True, remotes=[]) == 0
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "remote-old"


def test_deploy_remotes_at_cli_without_paths(tmp_path: Path, fake_hosts: Path) -> None:
    from myfiles.cli import main

    base_dir = tmp_path / "repo"
    base_dir.mkdir()
    place_remote(base_dir, "ender3", "etc/motd", "tracked")
    host_file(fake_hosts, "ender3", "etc/motd", "old")

    assert main(["deploy", "--remotes", "-d", str(base_dir)]) == 0
    assert (fake_hosts / "ender3" / "etc" / "motd").read_text() == "tracked"
