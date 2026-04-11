"""Allowed-path computation for agent sessions."""

from __future__ import annotations

import os


def compute_allowed_paths(
    session_cwd: str,
    *,
    user_data_path: str,
    bot_dir: str | None = None,
    worktrees_dir: str | None = None,
    admin_allowed_cwds: list[str] | None = None,
) -> list[str]:
    """Compute the allowed write paths for an agent session.

    Agents can always write to their cwd and the user data directory.
    Code agents (cwd inside bot_dir or worktrees_dir) additionally get
    access to the worktrees directory and admin-allowed paths.
    """
    allowed_cwd = os.path.realpath(session_cwd)
    user_data = os.path.realpath(user_data_path)
    paths = [allowed_cwd, user_data]

    if bot_dir and worktrees_dir:
        real_bot = os.path.realpath(bot_dir)
        real_worktrees = os.path.realpath(worktrees_dir)
        is_code_agent = allowed_cwd in (real_bot, real_worktrees) or allowed_cwd.startswith(
            (real_bot + os.sep, real_worktrees + os.sep)
        )
        if is_code_agent:
            paths.append(real_worktrees)
            if admin_allowed_cwds:
                paths.extend(admin_allowed_cwds)

    return paths
