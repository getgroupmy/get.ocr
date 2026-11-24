from __future__ import annotations


class ApiError(Exception):
    """Answered as {"error": code, "message": text} with the given HTTP status."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RequestTooLarge(Exception):
    """The request body went past the size cap while it was being read (the body-size
    middleware in app.main). Answered as 413."""
