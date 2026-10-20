"""Refusals: an LLM call a model API or gateway turned down for spend or rate.

Two stable error codes (docs-mint/admin/llm-gateway-attribution.mdx):

- `budget_exceeded` — a budget, quota or credit balance is exhausted: a
  gateway's per-customer or per-key budget, an account out of credits.
  Retrying won't help until someone raises the budget or it resets, so it
  is never retried, neither by Sinas nor by the SDK under it. HTTP 402.
- `rate_limited` — a 429 that isn't a budget refusal: too many requests now,
  fine later (the SDKs already retried it with backoff). HTTP 429.

Detection is deliberately conservative: the HTTP status and well-known error
shapes only, so an ordinary error is never mistaken for a refusal.

budget_exceeded:
- HTTP 402 Payment Required, whatever the body;
- an error `type` or `code` (OpenAI-style `error.type` / `error.code`, or
  top-level) of `budget_exceeded`, `insufficient_quota`, `billing_error` or
  `billing_hard_limit_reached`;
- an error message containing one of BUDGET_MESSAGE_MARKERS (gateway budget
  errors that come back under another type, such as a 401 or 403);
- or a provider's own rules (`config.budget_errors`), for any other gateway.

rate_limited: HTTP 429 that none of the above matched.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

BUDGET_EXCEEDED = "budget_exceeded"
RATE_LIMITED = "rate_limited"

# Sinas's own HTTP status for each code.
HTTP_STATUS = {BUDGET_EXCEEDED: 402, RATE_LIMITED: 429}

BUDGET_ERROR_TYPES = frozenset({
    "budget_exceeded",
    "insufficient_quota",
    "billing_error",
    "billing_hard_limit_reached",
})
# Lowercase substrings of an error message that only budget refusals use.
BUDGET_MESSAGE_MARKERS = (
    "exceededbudget",
    "exceededtokenbudget",
    "budget has been exceeded",
    "budget limit exceeded",
    "exceeded budget",
    "credit balance is too low",
    "insufficient credits",
)

MAX_RULES = 20
_RULE_KEYS = {"status", "type", "code", "message_contains"}


class LLMRefusalError(Exception):
    """A call the model API or a gateway refused for budget or rate.

    `str()` starts with the code (`budget_exceeded: ...`), so the code
    survives wherever only the text of an error is kept."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        upstream_status: Optional[int] = None,
        provider: Optional[str] = None,
        retry_after: Optional[str] = None,
    ):
        self.code = code
        self.message = message
        self.upstream_status = upstream_status
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{code}: {message}")

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 502)

    def to_dict(self) -> dict[str, Any]:
        return {"detail": str(self), "code": self.code}

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> "LLMRefusalError":
        """From a stream `error` event that carries a refusal `code`."""
        code = event.get("code") or BUDGET_EXCEEDED
        message = str(event.get("error") or code)
        prefix = f"{code}: "
        return cls(code, message[len(prefix):] if message.startswith(prefix) else message)


def refusal_code(value: Any) -> Optional[str]:
    """The refusal code of an exception or event, if it is one."""
    if isinstance(value, LLMRefusalError):
        return value.code
    code = value.get("code") if isinstance(value, dict) else None
    return code if code in HTTP_STATUS else None


# ── Rules ──────────────────────────────────────────────────────────────────


def validate_budget_rules(value: Any) -> list[dict[str, Any]]:
    """A provider's `config.budget_errors`: a list of match rules, each a
    combination of `status` (an int or a list of ints), `type`, `code` and
    `message_contains` (strings); a rule matches an error when every key it
    has matches. Raises ValueError naming the first problem."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("budget_errors must be a list of match rules")
    if len(value) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} budget_errors rules")
    out = []
    for i, rule in enumerate(value):
        where = f"budget_errors[{i}]"
        if not isinstance(rule, dict) or not rule:
            raise ValueError(f"{where} must be an object with status, type, code and/or message_contains")
        unknown = set(rule) - _RULE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        status = rule.get("status")
        if status is not None:
            statuses = status if isinstance(status, list) else [status]
            if not statuses or not all(
                isinstance(s, int) and not isinstance(s, bool) and 400 <= s <= 599 for s in statuses
            ):
                raise ValueError(f"{where}: status must be an HTTP error status (400-599) or a list of them")
        for key in ("type", "code", "message_contains"):
            if key in rule and (not isinstance(rule[key], str) or not rule[key].strip() or len(rule[key]) > 200):
                raise ValueError(f"{where}: {key} must be a non-empty string of at most 200 characters")
        out.append(dict(rule))
    return out


def _rule_matches(rule: dict[str, Any], status: Optional[int], types: set[str], message: str) -> bool:
    if "status" in rule:
        statuses = rule["status"] if isinstance(rule["status"], list) else [rule["status"]]
        if status not in statuses:
            return False
    if "type" in rule and rule["type"].lower() not in types:
        return False
    if "code" in rule and rule["code"].lower() not in types:
        return False
    if "message_contains" in rule and rule["message_contains"].lower() not in message:
        return False
    return True


# ── Classification ─────────────────────────────────────────────────────────


def _error_fields(body: Any) -> tuple[set[str], str]:
    """The lowercased type/code strings and the message of an error body:
    OpenAI-style `{"error": {"type", "code", "message"}}`, Anthropic's
    `{"type": "error", "error": {...}}`, or flat `{"detail" / "message"}`."""
    types: set[str] = set()
    messages: list[str] = []
    if isinstance(body, dict):
        for container in (body, body.get("error")):
            if not isinstance(container, dict):
                if isinstance(container, str):
                    messages.append(container)
                continue
            for key in ("type", "code"):
                value = container.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    types.add(str(value).lower())
            for key in ("message", "detail", "error_message"):
                value = container.get(key)
                if isinstance(value, str):
                    messages.append(value)
    elif isinstance(body, str):
        messages.append(body)
    return types, " ".join(messages).lower()


def classify(
    status: Optional[int], body: Any, message: str = "", rules: Optional[list[dict[str, Any]]] = None
) -> Optional[str]:
    """BUDGET_EXCEEDED, RATE_LIMITED or None for an error response."""
    types, body_message = _error_fields(body)
    text = f"{body_message} {(message or '').lower()}"
    for rule in rules or []:
        try:
            if _rule_matches(rule, status, types, text):
                return BUDGET_EXCEEDED
        except Exception:  # a rule written by hand into the database
            continue
    if status == 402:
        return BUDGET_EXCEEDED
    if types & BUDGET_ERROR_TYPES:
        return BUDGET_EXCEEDED
    if status is not None and status >= 400 and any(marker in text for marker in BUDGET_MESSAGE_MARKERS):
        return BUDGET_EXCEEDED
    if status == 429:
        return RATE_LIMITED
    return None


def _status_of(exc: BaseException) -> Optional[int]:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _body_of(exc: BaseException) -> Any:
    body = getattr(exc, "body", None)
    if body is not None:
        return body
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


def refusal_from_exception(
    exc: BaseException, rules: Optional[list[dict[str, Any]]] = None, provider: Optional[str] = None
) -> Optional[LLMRefusalError]:
    """The refusal an SDK or HTTP error stands for, or None for any other
    error (or one that already is a refusal)."""
    if isinstance(exc, LLMRefusalError):
        return None
    status = _status_of(exc)
    if status is None:
        return None
    body = _body_of(exc)
    raw_message = getattr(exc, "message", None) or str(exc)
    code = classify(status, body, raw_message, rules)
    if code is None:
        return None
    retry_after = None
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            retry_after = response.headers.get("retry-after")
        except Exception:
            retry_after = None
    reason = "budget or quota exhausted" if code == BUDGET_EXCEEDED else "rate limited"
    where = f"LLM provider '{provider}'" if provider else "The LLM provider"
    detail = str(raw_message)[:500]
    return LLMRefusalError(
        code,
        f"{where} refused the call ({reason}, HTTP {status}): {detail}",
        upstream_status=status,
        provider=provider,
        retry_after=retry_after,
    )


def parse_rules_header(value: Optional[str]) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        rules = json.loads(value)
        return rules if isinstance(rules, list) else []
    except Exception:
        return []


# ── HTTP ───────────────────────────────────────────────────────────────────


def install_refusal_handler(app: Any) -> None:
    """Answer a refusal that reaches the API as its own status and code
    (`{"detail": "budget_exceeded: ...", "code": "budget_exceeded"}`, 402),
    not as a generic 500."""
    from fastapi.responses import JSONResponse

    async def _handler(request: Any, exc: LLMRefusalError) -> JSONResponse:
        headers = {"Retry-After": exc.retry_after} if exc.code == RATE_LIMITED and exc.retry_after else None
        return JSONResponse(exc.to_dict(), status_code=exc.http_status, headers=headers)

    app.add_exception_handler(LLMRefusalError, _handler)
