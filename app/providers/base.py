"""Provider contract and sanitized error types."""

from typing import Protocol

from app.errors import ServiceError


class ProviderError(ServiceError):
    """An upstream model call failed without exposing its request or key."""


class ProviderTimeoutError(ProviderError):
    """An upstream model call exceeded its configured timeout."""


class QuestionProvider(Protocol):
    async def generate(self, prompt: str) -> str:
        """Generate a JSON string from the given prompt."""
        ...

    async def generate_grounded(self, prompt: str) -> str:
        """Generate grounded JSON including evidence citations."""
        ...

    async def aclose(self) -> None:
        """Release resources when the application shuts down."""
        ...
