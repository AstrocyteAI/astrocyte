"""Errors raised by :mod:`astrocyte_integration_tavus`."""


class TavusAPIError(Exception):
    """HTTP error from the Tavus API (4xx/5xx or unexpected response)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
