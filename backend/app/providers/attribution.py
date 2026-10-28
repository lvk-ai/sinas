"""Request headers from identity: an LLM provider's header templates.

A provider's `config.headers` maps a header name to a template, sent on every
call made through that provider (docs-mint/admin/llm-gateway-attribution.mdx).
A gateway in front of the model API (a spend-tracking proxy, an internal
router) reads them to attribute and budget the call:

    config:
      headers:
        x-customer-id: "{{attribution.orgId}}"
        x-request-tags: "agent:{{agent.namespace}}/{{agent.name}}"

A template is static text and a fixed set of variables, nothing else: no
expressions, filters or defaults. The values come from identity Sinas holds
(the chat's attribution snapshot, the user the turn runs as, the agent, the
chat, the API key it acts through), never from a request header a caller
sends.

Rules, each one a guarantee rather than a best effort:

- A header is sent whole or not at all. If any variable in its template
  resolves to nothing (unset, empty, not a scalar), or to a value that isn't
  safe in a header, the header is left out; the call still goes through.
  A half-resolved value (`org-` for `org-{{...}}`) would attribute the call
  to the wrong party, which is worse than not attributing it.
- Values are printable ASCII without `"` and `\\` (so a template may build a
  JSON header value), at most MAX_VALUE_LENGTH characters. CR, LF and every
  other control character are refused, so header injection is impossible.
- Header names are tokens, and reserved names (credentials, framing, the
  trace context the provider's `propagate_context` governs, the SDKs' own
  protocol headers) are refused when the provider is written, and again
  when a call is made.
- A template header wins over any other header of the same name a call
  would carry (`extra_params`, an SDK default); inbound request headers are
  never forwarded to a model API at all.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The variables a template may use. `attribution.<key>` (the chat's
# attribution snapshot) and `user.custom_fields.<key>` (the user's custom
# fields now) are families: any key that is a valid identifier.
TEMPLATE_VARIABLES = (
    "attribution.<key>",
    "user.id",
    "user.email",
    "user.custom_fields.<key>",
    "agent.namespace",
    "agent.name",
    "chat.id",
    "api_key.name",
)
_FIXED_VARIABLES = frozenset(v for v in TEMPLATE_VARIABLES if "<" not in v)
_CUSTOM_FIELD_PREFIX = "user.custom_fields."
_ATTRIBUTION_PREFIX = "attribution."
_KEYED_PREFIXES = {_CUSTOM_FIELD_PREFIX: "user.custom_fields", _ATTRIBUTION_PREFIX: "attribution"}
# Custom field keys a template (and an identity provider's claim mapping) can
# name: identifiers, like the query bind parameters built from them.
FIELD_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")

MAX_HEADERS = 20
MAX_TEMPLATE_LENGTH = 1024
MAX_VALUE_LENGTH = 1024

# Never settable by a template: credentials (the provider's key goes in its
# own field), message framing, and headers SDKs or proxies manage.
RESERVED_HEADERS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie",
    "host", "content-length", "content-type", "content-encoding", "transfer-encoding",
    "connection", "keep-alive", "te", "trailer", "upgrade", "expect", "accept",
    "accept-encoding", "user-agent", "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-real-ip", "idempotency-key", "x-should-retry",
    "ocp-apim-subscription-key", "x-portkey-api-key", "cf-aig-authorization",
    # Trace context: whether a call carries it is the provider's
    # `propagate_context` setting alone (clients.guard_trace_context).
    "traceparent", "tracestate", "baggage", "b3", "uber-trace-id", "sentry-trace",
    "x-cloud-trace-context", "grpc-trace-bin", "x-amzn-trace-id",
})
RESERVED_PREFIXES = (
    "x-sinas-",  # private markers that never leave the process
    "anthropic-",  # anthropic-version, anthropic-beta: the SDK's protocol
    "x-stainless-",  # the OpenAI/Anthropic SDKs' own
    "x-goog-",  # Google API key and upload protocol
    "x-amz-",  # AWS request signing
    "proxy-",
    "sec-",
    "x-b3-",
    "uberctx-",
    "ot-tracer-",
    "x-datadog-",
)


def _is_reserved(name: str) -> bool:
    lowered = name.lower()
    return lowered in RESERVED_HEADERS or lowered.startswith(RESERVED_PREFIXES)


def _printable(text: str) -> bool:
    return all(0x20 <= ord(c) <= 0x7E for c in text)


def template_variables(template: str) -> list[str]:
    """The variable names a template uses, in order."""
    return [m.group(1) for m in _PLACEHOLDER.finditer(template)]


def _check_variable(name: str) -> Optional[str]:
    """An error for a variable name a template may not use, else None."""
    if name in _FIXED_VARIABLES:
        return None
    for prefix in _KEYED_PREFIXES:
        if name.startswith(prefix):
            key = name[len(prefix):]
            if FIELD_KEY.match(key):
                return None
            return f"'{key}' is not a field key (letters, digits and _, not starting with a digit)"
    return f"unknown variable '{{{{{name}}}}}'; available: " + ", ".join(
        "{{" + v + "}}" for v in TEMPLATE_VARIABLES
    )


def validate_header_templates(value: Any) -> dict[str, str]:
    """A provider's `config.headers`, checked. Raises ValueError naming the
    first problem; returns the headers as given (names keep their case)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("headers must be an object of header name to template")
    if len(value) > MAX_HEADERS:
        raise ValueError(f"at most {MAX_HEADERS} headers")
    seen: set[str] = set()
    out: dict[str, str] = {}
    for name, template in value.items():
        if not isinstance(name, str) or not HEADER_NAME.match(name):
            raise ValueError(
                f"header name {name!r} is invalid: letters, digits and '-', at most 100 characters"
            )
        lowered = name.lower()
        if lowered in seen:
            raise ValueError(f"header '{name}' is set twice (header names ignore case)")
        seen.add(lowered)
        if _is_reserved(name):
            raise ValueError(
                f"header '{name}' is reserved: credentials, framing, trace context and the SDKs' "
                "own headers can't be set by a template"
            )
        if not isinstance(template, str):
            raise ValueError(f"header '{name}': the template must be a string")
        if not template.strip():
            raise ValueError(f"header '{name}': the template is empty")
        if len(template) > MAX_TEMPLATE_LENGTH:
            raise ValueError(f"header '{name}': the template is longer than {MAX_TEMPLATE_LENGTH} characters")
        if not _printable(template):
            raise ValueError(
                f"header '{name}': the template may hold printable ASCII only (no line breaks or control characters)"
            )
        static = _PLACEHOLDER.sub("", template)
        if "{{" in static or "}}" in static:
            raise ValueError(f"header '{name}': unbalanced '{{{{' or '}}}}' in the template")
        for variable in template_variables(template):
            problem = _check_variable(variable)
            if problem:
                raise ValueError(f"header '{name}': {problem}")
        out[name] = template
    return out


def clean_value(value: Any, *, max_length: int = MAX_VALUE_LENGTH) -> Optional[str]:
    """A variable's value as header-safe text, or None when it has none or
    isn't safe: a string, number or boolean whose text is non-empty printable
    ASCII without `"` or `\\`, at most `max_length` long. Never altered to
    fit (no truncation, no escaping): an unsafe value is no value."""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text or len(text) > max_length or not _printable(text) or '"' in text or "\\" in text:
        return None
    return text


def _lookup(variables: dict[str, Any], name: str) -> Any:
    for prefix, family in _KEYED_PREFIXES.items():
        if name.startswith(prefix):
            fields = variables.get(family)
            return fields.get(name[len(prefix):]) if isinstance(fields, dict) else None
    return variables.get(name)


def render_headers(templates: Any, variables: dict[str, Any]) -> dict[str, str]:
    """The headers to send: each template with its variables filled in, or
    left out whole when one of them has no safe value. A template that fails
    validation (a row written before a rule existed) is left out too."""
    if not templates or not isinstance(templates, dict):
        return {}
    try:
        checked = validate_header_templates(templates)
    except ValueError as e:
        logger.warning(f"LLM provider header templates ignored: {e}")
        return {}
    from .clients import trace_context_fields

    tracing = trace_context_fields()
    headers: dict[str, str] = {}
    for name, template in checked.items():
        if name.lower() in tracing:  # a propagator configured at runtime
            continue
        missing = False

        def fill(match: re.Match) -> str:
            nonlocal missing
            value = clean_value(_lookup(variables, match.group(1)))
            if value is None:
                missing = True
                return ""
            return value

        rendered = _PLACEHOLDER.sub(fill, template).strip()
        if missing or not rendered or len(rendered) > MAX_VALUE_LENGTH:
            continue
        headers[name] = rendered
    return headers


def merge_headers(base: Optional[dict[str, str]], *overrides: Optional[dict[str, str]]) -> dict[str, str]:
    """`base` with each override applied in turn; a later name replaces an
    earlier one whatever its case, so the request carries one value."""
    merged = dict(base or {})
    for override in overrides:
        for name, value in (override or {}).items():
            for existing in [k for k in merged if k.lower() == name.lower()]:
                del merged[existing]
            merged[name] = value
    return merged


# ── Attribution snapshot ───────────────────────────────────────────────────
#
# A chat takes a snapshot of its attribution fields when it is created
# (chats.attribution): ATTRIBUTION_FIELDS and USAGE_CUSTOMER_FIELD, read from
# the creating user's custom fields. Every LLM call in the chat is attributed
# by that snapshot, whoever continues the chat and however (an answer by
# someone else, a resume, a wakeup, a watch, background work); a sub-agent's
# chat inherits its parent's. A later change of the user's fields applies to
# chats created after it, never to a running one.

MAX_SNAPSHOT_KEYS = 20


def attribution_keys() -> list[str]:
    """The configured attribution fields: ATTRIBUTION_FIELDS, plus
    USAGE_CUSTOMER_FIELD. Keys that aren't identifiers are ignored."""
    from app.core.config import settings

    keys: list[str] = []
    # The customer field first: it is always part of the snapshot, however
    # many others are listed.
    for raw in [settings.usage_customer_field or "", *(settings.attribution_fields or "").split(",")]:
        key = raw.strip()
        if key and FIELD_KEY.match(key) and key not in keys:
            keys.append(key)
    return keys[:MAX_SNAPSHOT_KEYS]


def snapshot_from_fields(
    custom_fields: Optional[dict[str, Any]], context: Optional[dict[str, Any]] = None
) -> Optional[dict[str, Any]]:
    """The attribution snapshot of these custom fields: each configured key
    with a header-safe value, the credential's session context winning key
    by key (an organization chosen for this session over the one the user's
    fields hold). None when no attribution field is configured (nothing to
    take); {} when none of them is set."""
    keys = attribution_keys()
    if not keys:
        return None
    fields = custom_fields if isinstance(custom_fields, dict) else {}
    session = context if isinstance(context, dict) else {}
    snapshot: dict[str, Any] = {}
    for key in keys:
        value = session.get(key) if key in session else fields.get(key)
        if clean_value(value) is not None:
            snapshot[key] = value
    return snapshot


# ── Session context ────────────────────────────────────────────────────────
#
# A credential can carry a context: a small dict (`{"orgId": "org-2"}`) on a
# Sinas access and refresh token (set by token exchange, or by an identity
# provider's session claim mappings at sign-in) and on an API key (copied
# from the session that created it, or set by an admin). It is bound per
# request (app/core/auth.py) and wins over the user's fields when a chat
# takes its attribution snapshot, so one user can work for several
# organizations at once, one session (or key) each.

MAX_CONTEXT_KEYS = 20
MAX_CONTEXT_VALUE_LENGTH = 255


def validate_context(value: Any) -> dict[str, Any]:
    """A credential context, checked: identifier keys, header-safe scalar
    values. Raises ValueError naming the first problem; {} for none."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("context must be an object of field name to value")
    if len(value) > MAX_CONTEXT_KEYS:
        raise ValueError(f"context holds at most {MAX_CONTEXT_KEYS} fields")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not FIELD_KEY.match(key):
            raise ValueError(f"context field {key!r} is not a field name (letters, digits and _)")
        if isinstance(item, (dict, list)) or clean_value(item, max_length=MAX_CONTEXT_VALUE_LENGTH) is None:
            raise ValueError(
                f"context field '{key}' must be a non-empty string, number or boolean of printable "
                f"ASCII without quotes or backslashes, at most {MAX_CONTEXT_VALUE_LENGTH} characters"
            )
        out[key] = item.strip() if isinstance(item, str) else item
    return out


def context_or_none(value: Any) -> Optional[dict[str, Any]]:
    """A stored or claimed context as a dict, or None when it is empty or
    invalid (a token or key written before a rule existed carries none)."""
    try:
        return validate_context(value) or None
    except ValueError:
        return None


def membership_fields() -> dict[str, str]:
    """ATTRIBUTION_MEMBERSHIPS: context field -> the user custom field that
    lists the values the user may hold it at (`orgId:orgs`)."""
    from app.core.config import settings

    pairs: dict[str, str] = {}
    for item in (settings.attribution_memberships or "").split(","):
        key, _, field = item.partition(":")
        key, field = key.strip(), field.strip()
        if FIELD_KEY.match(key) and FIELD_KEY.match(field):
            pairs[key] = field
    return pairs


def memberships(custom_fields: Optional[dict[str, Any]]) -> dict[str, list[Any]]:
    """The values each membership-checked context field may take for a user
    with these custom fields."""
    fields = custom_fields if isinstance(custom_fields, dict) else {}
    out: dict[str, list[Any]] = {}
    for key, field in membership_fields().items():
        listed = fields.get(field)
        values = listed if isinstance(listed, list) else ([] if listed is None else [listed])
        out[key] = [v for v in values if isinstance(v, (str, int, float, bool))]
    return out


def membership_problem(context: Optional[dict[str, Any]], custom_fields: Optional[dict[str, Any]]) -> Optional[str]:
    """Why a context isn't one the user may hold, or None. Only the fields
    ATTRIBUTION_MEMBERSHIPS names are checked; a user without the listing
    field may hold none of their values."""
    if not context:
        return None
    allowed = memberships(custom_fields)
    for key, value in context.items():
        if key in allowed and value not in allowed[key]:
            return f"The user isn't a member for {key} '{value}'"
    return None


async def new_chat_attribution(
    db: Any, user_id: Any, *, parent_chat_id: Optional[Any] = None
) -> Optional[dict[str, Any]]:
    """The snapshot a new chat takes: its parent chat's (a sub-agent's chat),
    else the requesting credential's context over the user's attribution
    fields now. Never raises (None: taken at the chat's first LLM call
    instead)."""
    try:
        if parent_chat_id:
            from app.models.chat import Chat

            parent = await db.get(Chat, uuid.UUID(str(parent_chat_id)))
            if parent is not None and parent.attribution is not None:
                return dict(parent.attribution)
        if not attribution_keys() or not user_id:
            return None
        from app.core.auth import current_credential_context
        from app.models.user import User

        user = await db.get(User, uuid.UUID(str(user_id)))
        return snapshot_from_fields(
            user.custom_fields if user is not None else None, current_credential_context()
        )
    except Exception as e:
        logger.warning(f"Taking a chat's attribution snapshot failed: {e}")
        return None


async def _chat_snapshot(db: Any, chat_id: Any, user: Any) -> Optional[dict[str, Any]]:
    """The chat's snapshot. A chat without one (created before attribution
    fields were configured, or by a path that took none) takes it now, from
    its user, kept with the caller's next commit."""
    from app.models.chat import Chat

    try:
        chat_uuid = uuid.UUID(str(chat_id))
    except ValueError:
        chat_uuid = None
    chat = await db.get(Chat, chat_uuid) if chat_uuid else None
    if chat is None:
        return snapshot_from_fields(user.custom_fields if user is not None else None)
    if chat.attribution is None and user is not None:
        snapshot = snapshot_from_fields(user.custom_fields)
        if snapshot is not None:
            chat.attribution = snapshot
    return chat.attribution


# ── Identity ───────────────────────────────────────────────────────────────


def _uses(templates: dict[str, Any], prefix: str) -> bool:
    return any(
        v.startswith(prefix)
        for t in templates.values()
        if isinstance(t, str)  # a malformed stored template renders nothing
        for v in template_variables(t)
    )


async def resolve_attribution(
    db: Any,
    usage_context: Optional[dict[str, Any]],
    templates: Optional[dict[str, Any]],
) -> tuple[dict[str, str], Optional[str]]:
    """(headers, customer) for a call made with this usage context.

    The identity is the call's: the user the chat belongs to (the context's
    user_id), its agent and chat, the chat's attribution snapshot, and the
    API key the run acts through (the task's acting key, carried into queued
    jobs and resumes; only when that key is the user's own). A call outside
    a chat (a provider batch, the adapter's passthrough) is attributed by the
    user's attribution fields now. `customer` is the snapshot's
    USAGE_CUSTOMER_FIELD, recorded on the call's llm_usage row.

    Never raises: a lookup that fails sends no template headers (rather than
    some of them) and records no customer.
    """
    from app.core.config import settings

    context = usage_context or {}
    templates = templates if isinstance(templates, dict) else {}
    customer_field = (settings.usage_customer_field or "").strip() or None
    user_id = context.get("user_id")
    chat_id = context.get("chat_id")
    if not templates and not (customer_field and user_id):
        return {}, None

    try:
        wants_snapshot = bool(customer_field) or _uses(templates, "attribution.")
        variables: dict[str, Any] = {}
        user = None
        if user_id and (wants_snapshot or _uses(templates, "user.")):
            from app.models.user import User

            user = await db.get(User, uuid.UUID(str(user_id)))
        if user is not None:
            variables["user.id"] = str(user.id)
            variables["user.email"] = user.email
            variables["user.custom_fields"] = dict(user.custom_fields or {})
        elif user_id:
            variables["user.id"] = str(user_id)

        agent = context.get("agent")
        if isinstance(agent, str) and "/" in agent:
            variables["agent.namespace"], variables["agent.name"] = agent.split("/", 1)
        if chat_id:
            variables["chat.id"] = str(chat_id)

        snapshot: Optional[dict[str, Any]] = None
        if wants_snapshot:
            if chat_id:
                snapshot = await _chat_snapshot(db, chat_id, user)
            elif user is not None:
                from app.core.auth import current_credential_context

                snapshot = snapshot_from_fields(user.custom_fields, current_credential_context())
        variables["attribution"] = dict(snapshot or {})

        if user_id and _uses(templates, "api_key."):
            variables["api_key.name"] = await _acting_key_name(db, str(user_id))

        customer = None
        if customer_field and snapshot:
            customer = clean_value(snapshot.get(customer_field), max_length=255)
        return render_headers(templates, variables), customer
    except Exception as e:
        logger.warning(f"Resolving LLM call attribution failed; sending none: {e}")
        return {}, None



async def _acting_key_name(db: Any, user_id: str) -> Optional[str]:
    """The name of the API key this task acts through, when it is the
    user's own active key."""
    from app.core.auth import current_api_key_id
    from app.models.user import APIKey

    key_id = current_api_key_id()
    if not key_id:
        return None
    key = await db.get(APIKey, uuid.UUID(str(key_id)))
    if key is None or not key.is_active or str(key.user_id) != user_id:
        return None
    if key.expires_at is not None:
        from datetime import UTC, datetime

        if key.expires_at < datetime.now(UTC):
            return None
    return key.name
