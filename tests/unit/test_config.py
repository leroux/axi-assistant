"""Unit tests for config.get_model() / set_model()."""

from unittest.mock import patch

from axi.config import VALID_MODELS, get_model, set_model


class TestGetModel:
    def test_default_is_opus(self) -> None:
        with patch("axi.config._load_config", return_value={}):
            assert get_model() == "opus"

    def test_returns_configured_model(self) -> None:
        with patch("axi.config._load_config", return_value={"model": "sonnet"}):
            assert get_model() == "sonnet"


class TestSetModel:
    def test_valid_model(self) -> None:
        with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
            result = set_model("sonnet")
            assert result == ""

    def test_invalid_model(self) -> None:
        result = set_model("gpt-4")
        assert "Invalid model" in result

    def test_case_insensitive(self) -> None:
        with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
            result = set_model("HAIKU")
            assert result == ""

    def test_all_valid_models_accepted(self) -> None:
        for model in VALID_MODELS:
            with patch("axi.config._load_config", return_value={}), patch("axi.config._save_config"):
                result = set_model(model)
                assert result == "", f"Model '{model}' should be valid"
