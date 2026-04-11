"""Egress filter: scrub secrets from text before it reaches Discord.

Also provides path-based access control for file reads and uploads.
"""

from __future__ import annotations

import fnmatch
import os
import re

# ---------------------------------------------------------------------------
# Secret patterns
# ---------------------------------------------------------------------------

# Discord bot tokens: base64-encoded bot ID.timestamp.HMAC
_DISCORD_TOKEN_RE = re.compile(
    r"[MNO][a-zA-Z\d_-]{23,25}\.[a-zA-Z\d_-]{6}\.[a-zA-Z\d_-]{27,}"
)

# Discord webhook URLs (contain tokens in the URL path)
_DISCORD_WEBHOOK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[a-zA-Z\d_-]+"
)

# Anthropic API keys
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[a-zA-Z\d_-]{20,}")

# GitHub tokens (classic PATs, fine-grained PATs, OAuth tokens)
_GITHUB_TOKEN_RE = re.compile(
    r"(?:ghp_|gho_|ghs_|ghr_|github_pat_)[a-zA-Z\d_]{20,}"
)

# AWS access key IDs (always start with AKIA)
_AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")

# SSH private key blocks
_SSH_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
)

# Generic "secret" assignments in config/env style: KEY=<long-high-entropy-value>
# Matches both explicit names and wildcard suffixes like JWT_SECRET, DB_PASSWORD, etc.
_ENV_SECRET_RE = re.compile(
    # Explicit legacy names (backward compat)
    r"(?:DISCORD_TOKEN|SECRET_KEY|API_KEY|API_SECRET|ACCESS_TOKEN|AUTH_TOKEN"
    r"|ANTHROPIC_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|PRIVATE_KEY"
    # Wildcard suffix patterns: anything ending in _SECRET, _TOKEN, _KEY, _PASSWORD, _CREDENTIAL
    r"|\w+_SECRET|\w+_TOKEN|\w+_KEY|\w+_PASSWORD|\w+_CREDENTIAL)"
    r"\s*[=:]\s*\S{20,}",
    re.IGNORECASE,
)

# Credential URLs: protocol://user:password@host (database URLs, Redis, AMQP, etc.)
_CREDENTIAL_URL_RE = re.compile(
    r"://\w+:[^@\s]{3,}@",
)

# Generic token assignments in YAML/JSON config style: some_token: <value>
# or some_token = <value> — case-insensitive, requires 20+ char value
_GENERIC_TOKEN_RE = re.compile(
    r"\w*token\s*[:=]\s*\S{20,}",
    re.IGNORECASE,
)

# Bare JWT tokens: three base64url segments separated by dots, starting with eyJ
# (the base64 encoding of '{"' which begins every JWT header)
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
)

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_SSH_KEY_RE, "[REDACTED:private-key]"),
    (_DISCORD_WEBHOOK_RE, "[REDACTED:webhook-url]"),
    (_DISCORD_TOKEN_RE, "[REDACTED:discord-token]"),
    (_ANTHROPIC_KEY_RE, "[REDACTED:anthropic-key]"),
    (_GITHUB_TOKEN_RE, "[REDACTED:github-token]"),
    (_AWS_KEY_RE, "[REDACTED:aws-key]"),
    (_ENV_SECRET_RE, "[REDACTED:secret]"),
    (_CREDENTIAL_URL_RE, "[REDACTED:credential-url]"),
    (_GENERIC_TOKEN_RE, "[REDACTED:secret]"),
    (_JWT_RE, "[REDACTED:jwt]"),
]

# ---------------------------------------------------------------------------
# Discord snowflake ID filtering
# ---------------------------------------------------------------------------
# Known guild and channel IDs loaded at init time. These are replaced with
# generic placeholders to prevent leaking server structure.

_SNOWFLAKE_REPLACEMENTS: dict[str, str] = {}


def register_snowflakes(ids: dict[str, str]) -> None:
    """Register Discord snowflake IDs to filter.

    *ids* maps snowflake ID strings to replacement labels, e.g.
    ``{"1475248473682215214": "[guild]", "1479924707552923802": "[channel]"}``.
    """
    _SNOWFLAKE_REPLACEMENTS.update(ids)


def _scrub_snowflakes(text: str) -> str:
    """Replace registered snowflake IDs in text."""
    for snowflake, label in _SNOWFLAKE_REPLACEMENTS.items():
        text = text.replace(snowflake, label)
    return text


def scrub_secrets(text: str) -> str:
    """Replace known secret patterns and registered snowflake IDs in *text*."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if _SNOWFLAKE_REPLACEMENTS:
        text = _scrub_snowflakes(text)
    return text


# ---------------------------------------------------------------------------
# Path-based access control for sensitive files
# ---------------------------------------------------------------------------

_HOME = os.path.expanduser("~")

# Basename patterns — matched against the final filename component
_BLOCKED_BASENAMES = {".env", "test-config.json"}

# Prefix patterns — resolved path must NOT start with any of these
_BLOCKED_PREFIXES = [
    os.path.join(_HOME, ".ssh") + os.sep,
    os.path.join(_HOME, ".config", "axi") + os.sep,
]

# Glob-style patterns matched against the resolved path
_BLOCKED_GLOBS = [
    "/proc/*/environ",
]

# Exact basename matches for shell history files
_BLOCKED_HISTORY = {".bash_history", ".python_history", ".zsh_history"}


def is_path_blocked(path: str) -> bool:
    """Return True if *path* points to a sensitive file that should not be read or uploaded."""
    resolved = os.path.realpath(os.path.expanduser(path))
    basename = os.path.basename(resolved)

    if basename in _BLOCKED_BASENAMES:
        return True

    if basename in _BLOCKED_HISTORY:
        return True

    for prefix in _BLOCKED_PREFIXES:
        if resolved.startswith(prefix) or resolved == prefix.rstrip(os.sep):
            return True

    for glob_pat in _BLOCKED_GLOBS:
        if fnmatch.fnmatch(resolved, glob_pat):
            return True

    return False
