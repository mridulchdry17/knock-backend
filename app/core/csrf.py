from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths exempt from the X-Requested-With check.
# Browser-redirect auth bootstrap (login + Google callback) cannot send custom headers.
_EXEMPT_PREFIXES: tuple[str, ...] = ("/auth/", "/healthz", "/readyz", "/docs", "/redoc", "/openapi.json")
_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


class CSRFHeaderMiddleware(BaseHTTPMiddleware):
    """Enforces 'X-Requested-With: XMLHttpRequest' on state-changing API requests.

    Combined with a strict CORS allow-list, this defeats CSRF without requiring
    a CSRF token table — browsers will not attach the custom header on a cross-site
    form submission, and CORS preflight blocks unauthorized origins.
    """

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if request.method in _SAFE_METHODS:
            return await call_next(request)
        if request.url.path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)
        if request.headers.get("x-requested-with", "").lower() != "xmlhttprequest":
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "csrf_blocked",
                        "message": "Missing required header X-Requested-With.",
                    }
                },
            )
        return await call_next(request)
