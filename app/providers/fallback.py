"""Optional second-layer provider used only when the primary upstream is unavailable."""

import logging

from app.providers.base import ProviderUnavailableError, QuestionProvider

logger = logging.getLogger(__name__)

class FallbackProvider:
    """Try the fallback once, and only after a fast, explicit availability failure.

    Timeouts and invalid completions never trigger the fallback: the primary call may
    still be running upstream, so reissuing it would double quota use inside the same
    shared deadline owned by QuestionGenerator. The schema/citation validation stays
    in QuestionGenerator, so a fallback completion is validated like any other.
    """

    def __init__(self, primary: QuestionProvider, fallback: QuestionProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def generate(self, prompt: str) -> str:
        """Generate drafts, switching to the fallback if the primary is unavailable."""
        try:
            return await self._primary.generate(prompt)
        except ProviderUnavailableError:
            logger.warning("llm_fallback_used operation=generate")
            return await self._fallback.generate(prompt)

    async def generate_grounded(self, prompt: str) -> str:
        """Generate grounded drafts, switching to the fallback if the primary is unavailable."""
        try:
            return await self._primary.generate_grounded(prompt)
        except ProviderUnavailableError:
            logger.warning("llm_fallback_used operation=generate_grounded")
            return await self._fallback.generate_grounded(prompt)

    async def aclose(self) -> None:
        """Close both providers even if one cleanup fails."""
        try:
            await self._primary.aclose()
        finally:
            await self._fallback.aclose()
