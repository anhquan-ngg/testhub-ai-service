"""Select question generation independently of the Google embedding provider."""

from app.config import Settings
from app.providers.base import QuestionProvider
from app.providers.gemini import GeminiProvider
from app.providers.nine_router import NineRouterProvider


def create_provider(settings: Settings) -> QuestionProvider:
    """Build only the configured LLM; never silently fall back to another upstream."""
    if settings.llm_provider == "9router":
        return NineRouterProvider(settings)
    return GeminiProvider(settings)
