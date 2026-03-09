"""Pytest fixtures for Axi bot smoke tests."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from .helpers import Discord

AXI_PY_DIR = Path(__file__).parent.parent
WORKTREE_DIR = AXI_PY_DIR.parent
DATA_DIR = WORKTREE_DIR.parent / f"{WORKTREE_DIR.name}-data"
TEST_CONFIG = Path.home() / ".config/axi/test-config.json"
INSTANCE_NAME = "smoke-test"
INSTANCE_DIR = Path("/home/ubuntu/axi-tests/smoke-test")
DEFAULT_TIMEOUT = 120.0
SPAWN_TIMEOUT = 180.0

# Track whether the last test failed (used for recovery between tests)
_last_test_failed = False


def pytest_runtest_makereport(item, call):
    """Track test failures so the next test can recover if needed."""
    global _last_test_failed
    if call.when == "call" and call.excinfo is not None:
        _last_test_failed = True


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


def _restart_bot() -> None:
    """Restart the bot with a clean session (no --resume)."""
    (INSTANCE_DIR / ".master_session_id").unlink(missing_ok=True)
    subprocess.run(
        ["uv", "run", "python", "../axi_test.py", "restart", INSTANCE_NAME],
        cwd=str(AXI_PY_DIR),
        capture_output=True,
        timeout=30,
    )


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
            f"No .env file at {env_path}. Run `uv run --directory axi-py python ../axi_test.py up {INSTANCE_NAME}` first."
        )
    return _read_env(env_path)


def _resolve_bot_token(test_config: dict, instance_env: dict) -> str:
    """Resolve the bot token for the test instance.

    First checks DISCORD_TOKEN in .env, then falls back to resolving
    via test-slots.json → test-config.json (same as the Rust bot).
    """
    if "DISCORD_TOKEN" in instance_env:
        return instance_env["DISCORD_TOKEN"]
    slots_path = Path.home() / ".config/axi/.test-slots.json"
    slots = json.loads(slots_path.read_text())
    slot = slots.get(INSTANCE_NAME)
    if not slot:
        pytest.fail(f"No slot for '{INSTANCE_NAME}' in {slots_path}")
    token_id = slot["token_id"]
    return test_config["bots"][token_id]["token"]


@pytest.fixture(scope="session")
def discord(test_config, instance_env) -> Discord:
    """Session-scoped Discord helper using the test instance's bot token."""
    sender_token = test_config["defaults"]["sender_token"]
    bot_token = _resolve_bot_token(test_config, instance_env)
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

    Restarts the bot with a clean session to avoid slow --resume with
    accumulated context from previous test runs.
    """
    # Record latest message BEFORE restart so we can detect the NEW ready message
    latest = discord.latest_message_id(master_channel) or "0"
    _restart_bot()
    # Wait for the NEW ready notification (after the restart)
    text = discord.poll_history(master_channel, after=latest, check="ready", timeout=45.0)
    if "ready" not in text.lower():
        time.sleep(15)  # Extra grace period for slow starts

    msgs = discord.send_and_wait(
        master_channel,
        'Say exactly: WARMUP_OK',
        timeout=DEFAULT_TIMEOUT,
    )
    text = discord.bot_response_text(msgs)
    if "WARMUP_OK" not in text:
        pytest.fail(f"Warmup failed — bot did not respond correctly: {text[:200]}")
    # Let the bot fully sleep before tests start
    time.sleep(3)


@pytest.fixture(scope="session")
def data_dir():
    """Return the test instance's data directory path."""
    return str(DATA_DIR)


@pytest.fixture(autouse=True)
def _recover_after_failure(warmup, discord: Discord, master_channel: str):
    """Recover from a stuck bot after a failed test.

    If the previous test failed (e.g., a spawn test that timed out), the bot
    might be stuck processing. Restart it so the next test starts clean.
    """
    global _last_test_failed
    if _last_test_failed:
        _last_test_failed = False
        latest = discord.latest_message_id(master_channel) or "0"
        _restart_bot()
        discord.poll_history(master_channel, after=latest, check="ready", timeout=45.0)
        # Verify the bot is responsive
        msgs = discord.send_and_wait(
            master_channel, 'Say exactly: RECOVERY_OK', timeout=DEFAULT_TIMEOUT
        )
        text = discord.bot_response_text(msgs)
        if "RECOVERY_OK" not in text:
            pytest.fail("Bot did not recover after restart")
    return
