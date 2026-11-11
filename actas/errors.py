"""Typed errors mapped to RFC 8693 / RFC 6749 error responses."""


class TokenExchangeError(Exception):
    """An error that maps to an OAuth 2.0 error response (RFC 6749 section 5.2)."""

    def __init__(self, error: str, description: str, status_code: int = 400,
                 headers: dict | None = None):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code
        self.headers = headers or {}

    def as_response(self) -> dict:
        return {"error": self.error, "error_description": self.description}


def invalid_request(desc: str) -> TokenExchangeError:
    return TokenExchangeError("invalid_request", desc, 400)


def invalid_grant(desc: str) -> TokenExchangeError:
    return TokenExchangeError("invalid_grant", desc, 400)


def invalid_scope(desc: str) -> TokenExchangeError:
    return TokenExchangeError("invalid_scope", desc, 400)


def invalid_target(desc: str) -> TokenExchangeError:
    return TokenExchangeError("invalid_target", desc, 400)


def unauthorized_client(desc: str) -> TokenExchangeError:
    return TokenExchangeError("unauthorized_client", desc, 400)
