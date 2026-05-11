"""Provider-error classification — terminal vs transient.

Used by bench scripts to decide whether to abort a run on an exception
vs. retry/fall-through. Extracted from the v0.x ``locomo.py`` bench
runner because the same logic is needed by the PageIndex bench scripts
(``scripts/bench_pageindex_locomo.py``) and the v0.x runner is being
removed.

Public API: :func:`is_terminal_error`. Tracks LLM-provider conventions:

Transient (return False — bench continues, often after a retry):
- HTTP 429 *rate-limit* (per-minute throttle, recovers in seconds)
- HTTP 5xx (server-side blip)
- timeout / connection reset

Terminal (return True — abort the run, every call will fail the same way):
- 429 ``insufficient_quota`` (out of credits)
- 401 ``invalid_api_key`` (bad credential)
- account deactivated / hard billing limit
"""

from __future__ import annotations

_TERMINAL_ERROR_CODES: frozenset[str] = frozenset({
    "insufficient_quota",
    "invalid_api_key",
    "account_deactivated",
    "billing_hard_limit_reached",
})

# Fallback substring markers for providers that don't expose structured
# error codes (older OpenAI SDKs, third-party adapters). Compared against
# ``str(exc).lower()``. Kept narrow: the discriminator strings are
# specific enough that a question echoing them in a traceback is unlikely.
_TERMINAL_ERROR_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "invalid_api_key",
    "incorrect api key",
    "account_deactivated",
    "account is deactivated",
    "billing_hard_limit_reached",
    "you exceeded your current quota",
)


def _structured_error_code(exc: BaseException) -> str | None:
    """Pull a stable error code out of common SDK exception shapes.

    OpenAI SDK ≥ 1.x exposes both ``exc.code`` (string) and
    ``exc.body == {"error": {"code": ...}}``. Anthropic and other SDKs
    follow similar shapes. Returns the lowercased code string when found,
    otherwise None — caller falls through to the substring fallback.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code.lower()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            body_code = err.get("code")
            if isinstance(body_code, str) and body_code:
                return body_code.lower()
    return None


def is_terminal_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is unrecoverable (vs. a transient hiccup
    that retry / fallback can handle).

    Resolution order:
      1. Structured ``.code`` / ``.body['error']['code']`` (preferred —
         stable identifiers, no false positives on prose).
      2. Substring match on ``str(exc).lower()`` for older SDKs / adapters.
    """
    code = _structured_error_code(exc)
    if code is not None:
        return code in _TERMINAL_ERROR_CODES
    msg = str(exc).lower()
    return any(marker in msg for marker in _TERMINAL_ERROR_MARKERS)
