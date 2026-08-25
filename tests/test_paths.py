"""Tests for path mapping helpers."""

from _pytest.monkeypatch import MonkeyPatch

from myfiles.paths import (
    absolute,
    display_path,
    is_within,
    relative_to_target,
    target_to_relative,
)


def test_target_to_relative() -> None:
    assert target_to_relative("/etc/fstab") == "etc/fstab"
    assert target_to_relative("/fstab") == "fstab"
    assert target_to_relative("/home/user/.bashrc") == "home/user/.bashrc"


def test_relative_to_target() -> None:
    assert relative_to_target("etc/fstab") == "/etc/fstab"


def test_absolute_expands_home(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/testuser")
    assert absolute("~/.bashrc") == "/home/testuser/.bashrc"


def test_display_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/testuser")
    assert display_path("/home/testuser/.config/app.conf") == "~/.config/app.conf"
    assert display_path("/home/testuser") == "~"
    assert display_path("/etc/fstab") == "/etc/fstab"
    assert display_path("/home/testuser2/x") == "/home/testuser2/x"


def test_is_within() -> None:
    assert is_within("/etc", "/etc/fstab")
    assert is_within("/etc", "/etc")
    assert not is_within("/etc", "/usr/bin")
    assert not is_within("/usr", "/usr2/bin")
