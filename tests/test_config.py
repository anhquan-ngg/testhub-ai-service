"""Configuration validation must not print environment values or secrets."""

import pytest

from app.config import Settings


@pytest.mark.parametrize("name,value", [
    ("GEMINI_TIMEOUT_MS", "bad-value"),
    ("GENERATION_TIMEOUT_MS", "0"),
    ("GEMINI_MAX_ATTEMPTS", "6"),
    ("MAX_CONCURRENT_GENERATIONS", "0"),
])
def test_invalid_config_has_safe_error(monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert name in str(error.value)
    assert "bad-value" not in str(error.value)


def test_settings_repr_hides_secrets() -> None:
    settings = Settings(gemini_api_key="private-api-key", internal_api_token="private-token")
    assert "private-api-key" not in repr(settings)
    assert "private-token" not in repr(settings)
