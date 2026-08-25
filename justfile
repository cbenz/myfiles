default:
    @just --list --justfile {{justfile()}}

check: check-format lint check-types

check-format:
    uv run ruff format --check

check-types:
    uv run basedpyright

fix: format lint-fix

format:
    uv run ruff format

lint *args:
    uv run ruff check {{ args }}

lint-fix:
    uv run ruff check --fix-only

test:
    uv run pytest
