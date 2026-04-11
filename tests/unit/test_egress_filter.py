"""Tests for axi.egress_filter — secret scrubbing patterns."""

from __future__ import annotations

from axi.egress_filter import scrub_secrets


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
