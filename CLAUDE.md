# Development

## Setup

After cloning or creating a worktree, install git hooks:

    uv run pre-commit install

## Linting

Ruff runs automatically on commit via pre-commit hooks. To run manually:

    uv run ruff check --force-exclude .

Custom project-specific lint rules (LibCST + Fixit):

    uv run fixit lint .
    uv run fixit test .lint
