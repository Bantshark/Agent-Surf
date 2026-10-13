"""Fix 37: Anthropic API errors as one clean line, and checks on the stored key.

Every model call (learn, learn-action, self-heal, inbox) goes through ``call``.
An API error becomes ModelUnavailable, whose message names only the error class
and status: never the key, the request or its headers, nor the response body.
A ModelUnavailable is not a failed relearn: the map is not called broken by it.
"""

from __future__ import annotations

from typing import Any, Callable

KEY_PREFIX = "sk-ant-"
ENV_PREFIX = "ANTHROPIC_API_KEY="


class ModelUnavailable(RuntimeError):
    """The model could not be asked (bad key, rate limit, network, API error)."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def describe(e: BaseException) -> tuple[str, str] | None:
    """(kind, one-line message) for an Anthropic API error, else None."""
    try:
        import anthropic
    except ImportError:   # pragma: no cover - a pinned dependency
        return None
    if isinstance(e, anthropic.AuthenticationError):
        return "auth", ("the Anthropic key was rejected (401); run: agent-surf key set "
                        "(or set ANTHROPIC_API_KEY)")
    if isinstance(e, anthropic.PermissionDeniedError):
        return "permission", "the Anthropic key is not allowed to do this (403); check the key's workspace"
    if isinstance(e, anthropic.RateLimitError):
        return "rate_limit", "the Anthropic API rate limited the request (429); try again later"
    if isinstance(e, anthropic.APIConnectionError):   # includes timeouts
        return "connection", f"cannot reach the Anthropic API ({type(e).__name__})"
    if isinstance(e, anthropic.APIStatusError):
        status = getattr(e, "status_code", "?")
        return "status", f"the Anthropic API returned an error ({status} {type(e).__name__}); try again later"
    return None


def clean(e: BaseException) -> ModelUnavailable | None:
    found = describe(e)
    return ModelUnavailable(found[1], found[0]) if found else None


def call(fn: Callable[..., Any], /, **kw: Any) -> Any:
    """Call ``fn(**kw)`` (e.g. client.messages.create); API errors become
    ModelUnavailable, raised without the original as context (its text and
    its request are not shown)."""
    failure = None
    try:
        return fn(**kw)
    except Exception as e:
        failure = clean(e)
        if failure is None:
            raise
    raise failure


def test_key(client: Any) -> str:
    """One minimal authenticated call (list one model; no tokens used).
    Returns "ok" or raises ModelUnavailable."""
    call(client.models.list, limit=1)
    return "ok"


def key_problems(value: str) -> list[str]:
    """Why a pasted key looks wrong. Reasons never quote the key."""
    problems = []
    n = len(value)
    for times in (2, 3):
        if n and n % times == 0 and value == value[: n // times] * times:
            problems.append(f"it looks pasted {times} times (identical parts)")
            break
    if any(c.isspace() for c in value):
        problems.append("it contains whitespace")
    if any(c in value for c in "\"'`"):
        problems.append("it contains quotes")
    if value.upper().startswith(ENV_PREFIX):
        problems.append(f"it starts with {ENV_PREFIX!r}; paste only the value")
    elif not value.startswith(KEY_PREFIX):
        problems.append(f"it does not start with {KEY_PREFIX!r}")
    return problems
