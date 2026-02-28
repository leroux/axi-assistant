"""Pytest fixtures for Axi bot smoke tests."""

import json
from pathlib import Path

import pytest

from .helpers import Discord

WORKTREE_DIR = Path(__file__).parent.parent
DATA_DIR = WORKTREE_DIR.parent / f"{WORKTREE_DIR.name}-data"
TEST_CONFIG = Path.home() / ".config/axi/test-config.json"
INSTANCE_NAME = "smoke-test"
DEFAULT_TIMEOUT = 120.0
SPAWN_TIMEOUT = 180.0


def _load_test_config() -> dict:
    with open(TEST_CONFIG) as f:
        return json.load(f)


def _read_env(env_path: Path) -> dict:
    """Parse a .env file into a dict."""
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


@pytest.fixture(scope="session")
def test_config():
    """Load test-config.json."""
    return _load_test_config()


@pytest.fixture(scope="session")
def instance_env():
    """Return the .env vars for the test instance."""
    env_path = WORKTREE_DIR / ".env"
    if not env_path.exists():
        pytest.skip(
            f"No .env file at {env_path}. Run `uv run python axi_test.py up {INSTANCE_NAME}` first."
        )
    return _read_env(env_path)


@pytest.fixture(scope="session")
def discord(test_config, instance_env) -> Discord:
    """Session-scoped Discord helper using the test instance's bot token."""
    sender_token = test_config["defaults"]["sender_token"]
    bot_token = instance_env["DISCORD_TOKEN"]
    guild_id = instance_env["DISCORD_GUILD_ID"]

    d = Discord(
        bot_token=bot_token,
        sender_token=sender_token,
        guild_id=guild_id,
    )
    yield d
    d.close()


@pytest.fixture(scope="session")
def master_channel(discord: Discord) -> str:
    """Find and return the #axi-master channel ID."""
    ch_id = discord.find_channel("axi-master")
    if not ch_id:
        pytest.fail("Could not find #axi-master channel in test guild")
    return ch_id


@pytest.fixture(scope="session")
def warmup(discord: Discord, master_channel: str):
    """Send a warmup message to ensure the bot is awake before tests run.

    This is session-scoped so it only runs once.
    """
    msgs = discord.send_and_wait(
        master_channel,
        'Say exactly: WARMUP_OK',
        timeout=DEFAULT_TIMEOUT,
    )
    text = discord.bot_response_text(msgs)
    if "WARMUP_OK" not in text:
        pytest.fail(f"Warmup failed — bot did not respond correctly: {text[:200]}")


@pytest.fixture(scope="session")
def data_dir():
    """Return the test instance's data directory path."""
    return str(DATA_DIR)


# Auto-use warmup for all tests
@pytest.fixture(autouse=True)
def _ensure_warm(warmup):
    """Ensure bot is warmed up before every test."""
