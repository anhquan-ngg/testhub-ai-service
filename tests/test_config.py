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
    assert "private-router-key" not in repr(Settings(router_api_key="private-router-key"))


def test_gemini_remains_default_and_router_config_is_opt_in() -> None:
    assert Settings().llm_provider == "gemini"
    settings = Settings(llm_provider="9router", router_base_url="http://localhost:20128/v1",
                        router_api_key="fixture-key", router_model="provider/model")
    assert settings.gemini_api_key == ""
    assert settings.router_max_attempts == 1


@pytest.mark.parametrize("name,value", [
    ("LLM_PROVIDER", "private-invalid-provider"),
    ("ROUTER_RESPONSE_FORMAT", "private-invalid-mode"),
    ("ROUTER_TIMEOUT_MS", "private-invalid-timeout"),
    ("ROUTER_TIMEOUT_MS", "120001"),
    ("ROUTER_MAX_ATTEMPTS", "0"),
    ("ROUTER_MAX_ATTEMPTS", "6"),
    ("ROUTER_MAX_OUTPUT_TOKENS", "0"),
    ("ROUTER_MAX_OUTPUT_TOKENS", "65537"),
])
def test_router_env_errors_are_sanitized(monkeypatch, name, value) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert name in str(error.value)
    assert "private-invalid" not in str(error.value)


@pytest.mark.parametrize("url", [
    "", "ftp://localhost/v1", "http:///v1", "http://localhost",
    "http://private:secret@localhost/v1", "http://localhost/v1?key=secret",
    "http://localhost/v1#secret", "http://localhost:bad/v1", "http://localhost:0/v1",
    "http://local host/v1", "http://localhost/\nv1", "http://localhost/v1?",
    "http://localhost/v1#", "http://localhost\\private/v1",
])
def test_router_rejects_invalid_api_base_without_echoing_it(url) -> None:
    with pytest.raises(ValueError, match="ROUTER_BASE_URL") as error:
        Settings(llm_provider="9router", router_base_url=url,
                 router_api_key="private-key", router_model="model")
    assert "secret" not in str(error.value) and "private-key" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("router_api_key", ""), ("router_api_key", "private key"),
    ("router_api_key", "private\nkey"), ("router_api_key", "non-ascii-\u00e9"),
    ("router_model", ""), ("router_model", "  "), ("router_model", "private\nmodel"),
])
def test_router_requires_usable_credentials_and_model(field, value) -> None:
    values = dict(llm_provider="9router", router_base_url="http://localhost:20128/v1",
                  router_api_key="fixture-key", router_model="fixture-model")
    values[field] = value
    with pytest.raises(ValueError, match=field.upper()) as error:
        Settings(**values)
    assert "private" not in str(error.value)


def test_router_env_values_are_loaded_once(monkeypatch) -> None:
    values = dict(LLM_PROVIDER="9router", ROUTER_BASE_URL="http://localhost:20128/v1/",
                  ROUTER_API_KEY="fixture-key", ROUTER_MODEL="provider/model",
                  ROUTER_TIMEOUT_MS="15000", ROUTER_MAX_ATTEMPTS="2",
                  ROUTER_MAX_OUTPUT_TOKENS="4096", ROUTER_RESPONSE_FORMAT="json_object")
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.llm_provider == "9router" and settings.router_model == "provider/model"
    assert settings.router_api_key == "fixture-key" and settings.router_timeout_ms == 15000
    assert settings.router_max_attempts == 2 and settings.router_max_output_tokens == 4096
    assert settings.router_response_format == "json_object"


def test_router_rag_still_requires_google_embedding_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings(llm_provider="9router", router_base_url="http://localhost:20128/v1",
                 router_api_key="fixture-key", router_model="provider/model",
                 rag_enabled=True, rag_database_url="postgresql://fixture")

@pytest.mark.parametrize("override", [1, 110_000, 124_999])
def test_grounded_deadline_rejects_insufficient_headroom(override: int) -> None:
    """Explicit deadlines must leave retrieval/scope checks fifteen seconds."""
    with pytest.raises(ValueError, match="GROUNDED_GENERATION_TIMEOUT_MS"):
        Settings(generation_timeout_ms=110_000, grounded_timeout_ms=override)

@pytest.mark.parametrize("override", [125_000, 125_001])
def test_grounded_deadline_accepts_sufficient_headroom(override: int) -> None:
    """The exact headroom boundary and larger overrides remain usable."""
    settings = Settings(generation_timeout_ms=110_000, grounded_timeout_ms=override)
    assert settings.effective_grounded_timeout_ms == override

@pytest.mark.parametrize("override", [-1, 300_001])
def test_grounded_bounds_name_the_public_environment_variable(override: int) -> None:
    """Bounds failures identify the actual configurable environment key."""
    with pytest.raises(ValueError, match="GROUNDED_GENERATION_TIMEOUT_MS must be between 0 and 300000"):
        Settings(grounded_timeout_ms=override)


def test_fallback_and_grounded_deadline_config(monkeypatch) -> None:
    base = {"gemini_api_key": "gemini-test-key", "llm_provider": "9router", "router_api_key": "router-test-key", "router_model": "m", "router_base_url": "http://127.0.0.1:20128/v1"}
    assert Settings(**base).effective_grounded_timeout_ms == 75_000
    assert Settings(**base, generation_timeout_ms=110_000).effective_grounded_timeout_ms == 125_000
    assert Settings(**base, grounded_timeout_ms=90_000).effective_grounded_timeout_ms == 90_000
    assert Settings(**base, llm_fallback_provider="gemini").llm_fallback_provider == "gemini"
    for bad in (
        {"llm_fallback_provider": "other"},
        {"llm_fallback_provider": "gemini", "gemini_api_key": ""},
        {"grounded_timeout_ms": 300_001},
    ):
        with pytest.raises(ValueError):
            Settings(**{**base, **bad})
    with pytest.raises(ValueError):
        Settings(gemini_api_key="gemini-test-key", llm_fallback_provider="gemini")
    monkeypatch.setenv("LLM_FALLBACK_PROVIDER", "gemini")
    monkeypatch.setenv("GROUNDED_GENERATION_TIMEOUT_MS", "90000")
    monkeypatch.setenv("LLM_PROVIDER", "9router")
    monkeypatch.setenv("ROUTER_API_KEY", "router-test-key")
    monkeypatch.setenv("ROUTER_MODEL", "m")
    monkeypatch.setenv("ROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    loaded = Settings.from_env()
    assert loaded.llm_fallback_provider == "gemini" and loaded.grounded_timeout_ms == 90_000
