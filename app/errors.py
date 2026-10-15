"""Domain errors mapped to HTTP responses at the API boundary."""


class ServiceError(Exception):
    """An expected service failure with no sensitive payload."""


class ServiceUnavailableError(ServiceError):
    """The service is unconfigured or its capacity is currently exhausted."""
