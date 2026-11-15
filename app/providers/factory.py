"""Select question generation independently of the Google embedding provider."""

from app.config import Settings
from app.providers.base import QuestionProvider
from app.providers.fallback import FallbackProvider
from app.providers.gemini import GeminiProvider
from app.providers.nine_router import NineRouterProvider

def create_provider(settings: Settings) -> QuestionProvider:
    """Build the configured LLM; a fallback exists only when LLM_FALLBACK_PROVIDER opts in."""
    primary: QuestionProvider = (
        NineRouterProvider(settings) if settings.llm_provider == "9router" else GeminiProvider(settings)
    )
    if settings.llm_fallback_provider == "gemini":
        return FallbackProvider(primary, GeminiProvider(settings))
    return primary
