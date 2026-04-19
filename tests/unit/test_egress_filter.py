"""Tests for axi.egress_filter — secret scrubbing patterns."""

from __future__ import annotations

import os

import pytest

from axi import egress_filter
from axi.egress_filter import (
    register_literal_secrets,
    register_secrets_from_dir,
    scan_env_files,
    scrub_secrets,
)


@pytest.fixture(autouse=True)
def _reset_literal_secrets():
    """Snapshot and restore _LITERAL_SECRETS around each test."""
    saved = set(egress_filter._LITERAL_SECRETS)
    egress_filter._LITERAL_SECRETS.clear()
    try:
        yield
    finally:
        egress_filter._LITERAL_SECRETS.clear()
        egress_filter._LITERAL_SECRETS.update(saved)


# ---------------------------------------------------------------------------
# Previously leaked secrets — these MUST be caught
# ---------------------------------------------------------------------------


class TestLeakedSecrets:
    """Regression tests for secrets that actually leaked in production."""

    def test_jwt_secret(self):
        text = "JWT_SECRET=sezK6nXySgv/nXFX8ScV7ylQ5kyVx9YnURuhUTE6YsQ="
        assert "sezK6nXySgv" not in scrub_secrets(text)
        assert "[REDACTED:secret]" in scrub_secrets(text)

    def test_session_secret(self):
        text = "SESSION_SECRET=IULh3gzLabTiwzuIyuGL6Dd/pw3+orcXP1NVeRXQL1g="
        assert "IULh3gzLab" not in scrub_secrets(text)
        assert "[REDACTED:secret]" in scrub_secrets(text)

    def test_database_url_with_credentials(self):
        text = "DATABASE_URL=postgresql://postgres:KXZqYD5Essie7BZ@localhost:5433/mydb"
        result = scrub_secrets(text)
        assert "KXZqYD5Essie7BZ" not in result

    def test_repo_token_yaml_style(self):
        text = "repo_token: SIAeZjKYlHK74rbcFvNHMUzjRiMpflxve"
        assert "SIAeZjKYlHK74rbc" not in scrub_secrets(text)
        assert "[REDACTED:secret]" in scrub_secrets(text)


# ---------------------------------------------------------------------------
# Wildcard suffix patterns (_SECRET, _TOKEN, _KEY, _PASSWORD, _CREDENTIAL)
# ---------------------------------------------------------------------------


class TestWildcardSuffixes:
    """Env-style secrets with arbitrary prefixes."""

    def test_custom_secret(self):
        text = "MY_APP_SECRET=abcdef1234567890abcdef"
        assert "abcdef1234567890" not in scrub_secrets(text)

    def test_custom_token(self):
        text = "REFRESH_TOKEN=xya9284jf02kd93mf02kd93mf02kd93m"
        assert "xya9284jf02kd93m" not in scrub_secrets(text)

    def test_custom_key(self):
        text = "ENCRYPTION_KEY=superSecretKeyValue12345678"
        assert "superSecretKeyValue" not in scrub_secrets(text)

    def test_custom_password(self):
        text = "DB_PASSWORD=p4ssw0rd!@#$%^&*()_+longvalue"
        assert "p4ssw0rd!@#$%^&*" not in scrub_secrets(text)

    def test_custom_credential(self):
        text = "SERVICE_CREDENTIAL=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.long"
        assert "eyJhbGciOiJIUzI1Ni" not in scrub_secrets(text)

    def test_lowercase_suffix(self):
        text = "jwt_secret=sezK6nXySgv/nXFX8ScV7ylQ5kyVx9YnURuhUTE6YsQ="
        assert "sezK6nXySgv" not in scrub_secrets(text)


# ---------------------------------------------------------------------------
# Credential URL pattern
# ---------------------------------------------------------------------------


class TestCredentialURLs:
    """Database/service URLs with embedded credentials."""

    def test_postgres_url(self):
        text = "postgresql://postgres:KXZqYD5Essie7BZ@localhost:5433/mydb"
        result = scrub_secrets(text)
        assert "KXZqYD5Essie7BZ" not in result
        assert "[REDACTED:credential-url]" in result

    def test_redis_url(self):
        text = "redis://default:myS3cretPass@redis.example.com:6379/0"
        result = scrub_secrets(text)
        assert "myS3cretPass" not in result

    def test_amqp_url(self):
        text = "amqp://user:longpassword123@rabbit.host:5672/vhost"
        result = scrub_secrets(text)
        assert "longpassword123" not in result

    def test_mysql_url(self):
        text = "mysql://admin:hunter2isnotgood@db.internal:3306/app"
        result = scrub_secrets(text)
        assert "hunter2isnotgood" not in result


# ---------------------------------------------------------------------------
# Generic token pattern (YAML/JSON config style)
# ---------------------------------------------------------------------------


class TestGenericTokenPattern:
    """Catches token: <value> and token = <value> patterns."""

    def test_token_colon_yaml(self):
        text = "repo_token: SIAeZjKYlHK74rbcFvNHMUzjRiMpflxve"
        assert "SIAeZjKYlHK74rbc" not in scrub_secrets(text)

    def test_token_equals(self):
        text = "auth_token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        assert "eyJhbGciOiJIUzI1Ni" not in scrub_secrets(text)

    def test_camel_case_token(self):
        text = "apiToken: sk-1234567890abcdef1234567890abcdef"
        assert "sk-1234567890abcdef" not in scrub_secrets(text)

    def test_token_short_value_not_matched(self):
        """Values under 20 chars should NOT be matched by the generic token pattern."""
        text = "my_token: short"
        assert scrub_secrets(text) == text


# ---------------------------------------------------------------------------
# Bare JWT token pattern
# ---------------------------------------------------------------------------


class TestJWTPattern:
    """Catches bare JWT tokens (eyJ... three-segment base64url)."""

    def test_full_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result = scrub_secrets(jwt)
        assert "eyJhbGciOiJ" not in result
        assert "[REDACTED:jwt]" in result

    def test_jwt_in_header(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result = scrub_secrets(text)
        assert "eyJhbGciOiJ" not in result

    def test_jwt_in_env_value(self):
        text = "AUTH=eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiYWRtaW4ifQ.kP3LMz1OPz5GpN7XxzK2rVoSzHTf0Y6CJjK"
        result = scrub_secrets(text)
        assert "eyJhbGciOiJSUzI1Ni" not in result

    def test_short_base64_not_matched(self):
        """Short eyJ strings that aren't real JWTs should not match."""
        text = "eyJhbG.eyJz.abc"
        assert scrub_secrets(text) == text


# ---------------------------------------------------------------------------
# Existing patterns still work (backward compat)
# ---------------------------------------------------------------------------


class TestExistingPatterns:
    """Ensure pre-existing patterns still fire correctly."""

    def test_discord_token(self):
        # Fake token matching Discord bot token format
        text = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GwPQ4g.abcdefghijklmnopqrstuvwxyz1"
        assert "[REDACTED:discord-token]" in scrub_secrets(text)

    def test_anthropic_key(self):
        text = "sk-ant-abcdefghijklmnopqrstuv"
        assert "[REDACTED:anthropic-key]" in scrub_secrets(text)

    def test_github_pat(self):
        text = "ghp_ABCDEFGHIJKLMNOPQRSTuvwx"
        assert "[REDACTED:github-token]" in scrub_secrets(text)

    def test_aws_key(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        assert "[REDACTED:aws-key]" in scrub_secrets(text)

    def test_ssh_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nblah\n-----END RSA PRIVATE KEY-----"
        assert "[REDACTED:private-key]" in scrub_secrets(text)

    def test_discord_webhook(self):
        text = "https://discord.com/api/webhooks/1234567890/abcdeftoken"
        assert "[REDACTED:webhook-url]" in scrub_secrets(text)

    def test_legacy_env_discord_token(self):
        text = "DISCORD_TOKEN=MTIzNDU2Nzg5MDEyMzQ1Njc4OTA.long-token-value-here"
        result = scrub_secrets(text)
        # Should be caught by at least one pattern
        assert "long-token-value-here" not in result

    def test_legacy_env_api_key(self):
        text = "API_KEY=sk-1234567890abcdef1234567890abcdef"
        assert "sk-1234567890abcdef" not in scrub_secrets(text)


# ---------------------------------------------------------------------------
# False positive checks — normal content should NOT be redacted
# ---------------------------------------------------------------------------


class TestFalsePositives:
    """Ensure common non-secret content is not redacted."""

    def test_git_hash(self):
        text = "commit af01001c3b4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a"
        assert scrub_secrets(text) == text

    def test_uuid(self):
        text = "id: 550e8400-e29b-41d4-a716-446655440000"
        assert scrub_secrets(text) == text

    def test_normal_url_no_credentials(self):
        text = "https://example.com/api/v1/users?page=2"
        assert scrub_secrets(text) == text

    def test_base64_in_code(self):
        text = 'encoded = base64.b64encode(b"hello world")'
        assert scrub_secrets(text) == text

    def test_regular_assignment(self):
        text = "MAX_RETRIES=5"
        assert scrub_secrets(text) == text

    def test_short_env_value(self):
        text = "SECRET_KEY=short"
        assert scrub_secrets(text) == text

    def test_url_without_password(self):
        text = "postgresql://localhost:5432/mydb"
        assert scrub_secrets(text) == text

    def test_generic_word_token_in_prose(self):
        """The word 'token' in normal English should not trigger redaction."""
        text = "The token count was 1500 for this request."
        assert scrub_secrets(text) == text

    def test_empty_string(self):
        assert scrub_secrets("") == ""

    def test_none_like_empty(self):
        assert scrub_secrets("") == ""

    def test_file_path_with_key(self):
        text = "Reading file /home/user/.ssh/id_rsa.pub"
        assert scrub_secrets(text) == text

    def test_discussion_about_tokens(self):
        text = "We need to refresh the token before it expires."
        assert scrub_secrets(text) == text


# ---------------------------------------------------------------------------
# Literal secret allowlist (loaded from .env files)
# ---------------------------------------------------------------------------


class TestLiteralSecrets:
    """register_literal_secrets + scrub_secrets literal-replace path."""

    def test_registered_value_replaced(self):
        register_literal_secrets(["FakePass1234567"])
        result = scrub_secrets("DB password: `FakePass1234567`")
        assert "FakePass1234567" not in result
        assert "[REDACTED:secret]" in result

    def test_short_values_not_registered(self):
        # Values <8 chars must be ignored to avoid mass-redacting common tokens.
        added = register_literal_secrets(["true", "5432", "ok", "1234567"])
        assert added == 0
        text = "host=localhost port=5432 debug=true"
        assert scrub_secrets(text) == text

    def test_eight_char_value_is_registered(self):
        # 8 chars is the minimum; verify the boundary.
        added = register_literal_secrets(["abcdefgh"])
        assert added == 1
        result = scrub_secrets("value=abcdefgh")
        assert "abcdefgh" not in result

    def test_empty_string_not_registered(self):
        added = register_literal_secrets(["", "valid_value_long_enough"])
        assert added == 1

    def test_overlapping_values_longest_first(self):
        # If "abc12345" and "abc12345_extra" are both registered, the longer
        # one must be replaced first or the shorter would clobber its prefix.
        register_literal_secrets(["abc12345", "abc12345_extra"])
        result = scrub_secrets("value=abc12345_extra,other=abc12345")
        assert "abc12345_extra" not in result
        assert "abc12345" not in result
        assert result.count("[REDACTED:secret]") == 2

    def test_literal_runs_before_regex(self):
        # A literal value that would otherwise trip a generic regex must be
        # caught by the literal pass first (still produces [REDACTED:secret]).
        register_literal_secrets(["FakePass1234567"])
        result = scrub_secrets("FakePass1234567")
        assert result == "[REDACTED:secret]"

    def test_unregistered_short_value_passes(self):
        result = scrub_secrets("just some normal text")
        assert result == "just some normal text"


# ---------------------------------------------------------------------------
# .env file scanner
# ---------------------------------------------------------------------------


class TestEnvScanner:
    """scan_env_files + register_secrets_from_dir."""

    def test_basic_env_file(self, tmp_path):
        (tmp_path / ".env").write_text("DB_PASS=FakePass1234567\nDEBUG=true\n")
        values = scan_env_files(str(tmp_path))
        assert "FakePass1234567" in values
        # short value still appears at the scan layer; filtering is applied at
        # register_literal_secrets time.
        assert "true" in values

    def test_skips_example_template_sample_dist(self, tmp_path):
        (tmp_path / ".env.example").write_text("API_KEY=ExampleValue1234\n")
        (tmp_path / ".env.template").write_text("API_KEY=TemplateValue5678\n")
        (tmp_path / ".env.sample").write_text("API_KEY=SampleValue9012\n")
        (tmp_path / ".env.dist").write_text("API_KEY=DistValue3456\n")
        values = scan_env_files(str(tmp_path))
        assert "ExampleValue1234" not in values
        assert "TemplateValue5678" not in values
        assert "SampleValue9012" not in values
        assert "DistValue3456" not in values

    def test_strips_double_quotes(self, tmp_path):
        (tmp_path / ".env").write_text('SECRET="QuotedValue1234"\n')
        values = scan_env_files(str(tmp_path))
        assert "QuotedValue1234" in values
        assert '"QuotedValue1234"' not in values

    def test_strips_single_quotes(self, tmp_path):
        (tmp_path / ".env").write_text("SECRET='QuotedValue1234'\n")
        values = scan_env_files(str(tmp_path))
        assert "QuotedValue1234" in values
        assert "'QuotedValue1234'" not in values

    def test_skips_comments_and_blank_lines(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# this is a comment\n"
            "\n"
            "   \n"
            "REAL_KEY=RealValue1234567\n"
            "  # indented comment\n"
        )
        values = scan_env_files(str(tmp_path))
        assert values == {"RealValue1234567"}

    def test_handles_export_prefix(self, tmp_path):
        (tmp_path / ".env").write_text("export API_TOKEN=ExportedValue1234\n")
        values = scan_env_files(str(tmp_path))
        assert "ExportedValue1234" in values

    def test_recurses_into_subdirs(self, tmp_path):
        # Mirrors minflow's api/.env layout that caused the actual leak.
        api = tmp_path / "api"
        api.mkdir()
        (api / ".env").write_text("DB_PASSWORD=NestedSecret1234\n")
        values = scan_env_files(str(tmp_path))
        assert "NestedSecret1234" in values

    def test_skips_excluded_dirs(self, tmp_path):
        for skipped in (".venv", "node_modules", ".git", "__pycache__"):
            d = tmp_path / skipped
            d.mkdir()
            (d / ".env").write_text(f"SHOULD_SKIP=skipped_{skipped}_value\n")
        values = scan_env_files(str(tmp_path))
        for skipped in (".venv", "node_modules", ".git", "__pycache__"):
            assert f"skipped_{skipped}_value" not in values

    def test_finds_dotted_variants(self, tmp_path):
        (tmp_path / ".env.local").write_text("LOCAL=LocalValue1234567\n")
        (tmp_path / ".env.production").write_text("PROD=ProdValue123456789\n")
        values = scan_env_files(str(tmp_path))
        assert "LocalValue1234567" in values
        assert "ProdValue123456789" in values

    def test_missing_dir_returns_empty(self, tmp_path):
        assert scan_env_files(str(tmp_path / "does_not_exist")) == set()

    def test_register_secrets_from_dir_end_to_end(self, tmp_path):
        # Full pipeline: write .env, scan + register, scrub a message.
        (tmp_path / ".env").write_text("DB_PASS=FakePass1234567\nDEBUG=true\n")
        api = tmp_path / "api"
        api.mkdir()
        (api / ".env").write_text("PG_PASS=NestedSecret1234\n")
        (tmp_path / ".env.example").write_text("DB_PASS=do_not_register_me\n")

        added = register_secrets_from_dir(str(tmp_path))
        assert added == 2  # FakePass1234567 + NestedSecret1234 (true + example skipped)

        msg = (
            "Connecting with DB password: `FakePass1234567` and "
            "PG password: `NestedSecret1234`. Should not register: do_not_register_me."
        )
        result = scrub_secrets(msg)
        assert "FakePass1234567" not in result
        assert "NestedSecret1234" not in result
        # the example value was never registered, so it survives
        assert "do_not_register_me" in result
        assert result.count("[REDACTED:secret]") == 2

    def test_absolute_paths_not_registered(self, tmp_path):
        # Absolute paths look like secrets to the scanner but are config, not
        # credentials — redacting them mangles normal output like file paths.
        (tmp_path / ".env").write_text(
            "SECRET=SomeRealSecret123\n"
            "DEFAULT_CWD=/home/user/project\n"
            "AXI_USER_DATA=/var/lib/axi\n"
        )
        added = register_secrets_from_dir(str(tmp_path))
        assert added == 1  # only SomeRealSecret123 registered
        assert "SomeRealSecret123" in egress_filter._LITERAL_SECRETS
        assert "/home/user/project" not in egress_filter._LITERAL_SECRETS
        assert "/var/lib/axi" not in egress_filter._LITERAL_SECRETS

        # Bonus: the path strings pass through scrub_secrets unchanged.
        msg = "cwd=/home/user/project data=/var/lib/axi"
        assert scrub_secrets(msg) == msg
