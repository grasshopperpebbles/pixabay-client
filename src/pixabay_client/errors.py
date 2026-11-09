"""Errors. Messages never contain the API key."""
from __future__ import annotations


class PixabayError(Exception):
    """Base class for every error this client raises."""


class AuthError(PixabayError):
    """The API key is missing or invalid."""


class NotFound(PixabayError):
    """No image or video with that id."""


class RateLimited(PixabayError):
    """Still rate limited after the retries (or the wait would exceed max_wait)."""

    def __init__(self, message: str, retry_after: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceError(PixabayError):
    """Pixabay answered 5xx, sent something unreadable, or could not be reached."""
