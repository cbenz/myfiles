"""Shared pytest configuration and fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolate_myfiles_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test independent of the developer's shell environment.

    ``MYFILES_BASE_DIR`` is commonly exported in the shell, pointing at the
    real dotfiles repository. Without clearing it, the base-dir resolution
    tests and the CLI tests would silently operate on the real repository
    instead of on their fixture repository (or erroring as expected). Tests
    that need the variable set it explicitly with ``monkeypatch.setenv``.
    """
    monkeypatch.delenv("MYFILES_BASE_DIR", raising=False)
