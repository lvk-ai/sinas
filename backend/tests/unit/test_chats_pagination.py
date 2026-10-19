"""GET /chats: keyset pagination, filters, last message, backward compatibility.

Without `limit` or `cursor` the endpoint returns every matching chat, as it
always did, up to CHATS_UNPAGINATED_MAX: past that it returns the first
ones, says so in X-Truncated, and carries the cursor for the rest. With
either, it returns one page and, when more follow, the cursor for the next
one in the X-Next-Cursor header.
"""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.runtime.endpoints.chats import CHATS_PAGE_DEFAULT, CHATS_PAGE_MAX, encode_chat_cursor
from app.core.config import Settings, settings
from app.models.chat import Chat, Message
from tests.conftest import auth_headers

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _chat(db, user, updated_at, title="chat", archived=False, chat_id=None):
    chat = Chat(
        id=chat_id or uuid.uuid4(),
        user_id=user.id,
        agent_namespace="ns",
        agent_name="bot",
        title=title,
        archived=archived,
        created_at=updated_at,
        updated_at=updated_at,
    )
    db.add(chat)
    await db.flush()
    return chat


def _expected(chats):
    """The endpoint's order: newest updated_at first, id breaking ties."""
    return [str(c.id) for c in sorted(chats, key=lambda c: (c.updated_at, c.id), reverse=True)]


async def _all_pages(client, user, params):
    """Follow X-Next-Cursor to the end; return (ids, number of pages)."""
    ids, pages, cursor = [], 0, None
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        r = await client.get("/chats", params=query, headers=auth_headers(user))
        assert r.status_code == 200, r.text
        pages += 1
        ids += [c["id"] for c in r.json()]
        cursor = r.headers.get("x-next-cursor")
        if not cursor:
            return ids, pages
        assert pages < 100, "pagination does not terminate"


async def test_pages_cover_every_chat_once_with_equal_timestamps(client, db, test_user):
    # Seven chats share one timestamp: only the id tiebreak keeps them apart.
    chats = [await _chat(db, test_user, BASE) for _ in range(7)]
    chats += [await _chat(db, test_user, BASE + timedelta(minutes=i)) for i in range(1, 6)]
    chats.append(await _chat(db, test_user, BASE - timedelta(microseconds=1)))

    ids, pages = await _all_pages(client, test_user, {"limit": 3})

    assert ids == _expected(chats)
    assert len(ids) == len(set(ids)) == 13
    assert pages == 5  # 3+3+3+3+1


async def test_last_page_has_no_cursor_even_when_full(client, db, test_user):
    for i in range(4):
        await _chat(db, test_user, BASE + timedelta(seconds=i))
    r = await client.get("/chats", params={"limit": 4}, headers=auth_headers(test_user))
    assert len(r.json()) == 4
    assert "x-next-cursor" not in r.headers
    assert "link" not in r.headers


async def test_insert_while_paging_neither_duplicates_nor_skips(client, db, test_user):
    chats = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(6)]
    h = auth_headers(test_user)

    r1 = await client.get("/chats", params={"limit": 2}, headers=h)
    first = [c["id"] for c in r1.json()]
    # A new chat lands at the front while the client is between pages.
    await _chat(db, test_user, BASE + timedelta(days=1), title="new")
    r2 = await client.get(
        "/chats", params={"limit": 2, "cursor": r1.headers["x-next-cursor"]}, headers=h
    )
    r3 = await client.get(
        "/chats", params={"limit": 2, "cursor": r2.headers["x-next-cursor"]}, headers=h
    )

    seen = first + [c["id"] for c in r2.json()] + [c["id"] for c in r3.json()]
    assert seen == _expected(chats)
    assert "x-next-cursor" not in r3.headers


async def test_archived_filter_with_cursor(client, db, test_user):
    live = [await _chat(db, test_user, BASE + timedelta(seconds=2 * i)) for i in range(5)]
    archived = [
        await _chat(db, test_user, BASE + timedelta(seconds=2 * i + 1), archived=True)
        for i in range(5)
    ]

    ids, _ = await _all_pages(client, test_user, {"limit": 2})
    assert ids == _expected(live)

    ids, _ = await _all_pages(client, test_user, {"limit": 3, "include_archived": "true"})
    assert ids == _expected(live + archived)


async def test_search_with_cursor_and_link_header(client, db, test_user):
    hits = [
        await _chat(db, test_user, BASE + timedelta(seconds=i), title=f"Quarterly Report {i}")
        for i in range(5)
    ]
    for i in range(5):
        await _chat(db, test_user, BASE + timedelta(seconds=10 + i), title=f"other {i}")

    ids, _ = await _all_pages(client, test_user, {"limit": 2, "q": "report"})
    assert ids == _expected(hits)

    r = await client.get(
        "/chats", params={"limit": 2, "q": "report"}, headers=auth_headers(test_user)
    )
    link = r.headers["link"]
    assert link.startswith("</chats?") and link.endswith('>; rel="next"')
    assert "q=report" in link and "limit=2" in link
    assert f"cursor={r.headers['x-next-cursor']}" in link


async def test_search_treats_wildcards_literally(client, db, test_user):
    await _chat(db, test_user, BASE, title="100% done")
    await _chat(db, test_user, BASE, title="1000 done")
    r = await client.get("/chats", params={"q": "0%"}, headers=auth_headers(test_user))
    assert [c["title"] for c in r.json()] == ["100% done"]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor(payload) -> str:
    return _b64(json.dumps(payload).encode())


_U = BASE.isoformat()
_I = str(uuid.uuid4())

INVALID_CURSORS = {
    # Not base64, not UTF-8, not JSON
    "bad-base64": "not-base64!!",
    "one-char": "A",
    "non-ascii": "é" * 8,
    "not-utf8": _b64(b"\xff\xfe\xfd\xfc"),
    "not-json": _b64(b"not json"),
    "too-long": "A" * 300,
    # JSON, but not an object
    "list": _cursor([_U, _I]),
    "string": _cursor("x"),
    "number": _cursor(1),
    "null": _cursor(None),
    # Missing or extra keys
    "empty-object": _cursor({}),
    "no-i": _cursor({"u": _U}),
    "no-u": _cursor({"i": _I}),
    "extra-key": _cursor({"u": _U, "i": _I, "x": 1}),
    # Wrong types for i
    "i-int": _cursor({"u": _U, "i": 123}),
    "i-null": _cursor({"u": _U, "i": None}),
    "i-list": _cursor({"u": _U, "i": [_I]}),
    "i-object": _cursor({"u": _U, "i": {"hex": _I}}),
    "i-bool": _cursor({"u": _U, "i": True}),
    # Wrong types for u
    "u-int": _cursor({"u": 1767225600, "i": _I}),
    "u-null": _cursor({"u": None, "i": _I}),
    "u-list": _cursor({"u": [_U], "i": _I}),
    "u-object": _cursor({"u": {"t": _U}, "i": _I}),
    # Right types, wrong values
    "u-not-a-date": _cursor({"u": "yesterday", "i": _I}),
    "i-not-a-uuid": _cursor({"u": _U, "i": "nope"}),
    # A timestamp without a timezone is ambiguous against timestamptz.
    "u-naive": _cursor({"u": "2026-01-01T00:00:00", "i": _I}),
    # Before year 1 once in UTC.
    "u-out-of-range": _cursor({"u": "0001-01-01T00:00:00+14:00", "i": _I}),
}


@pytest.mark.parametrize("cursor", INVALID_CURSORS.values(), ids=INVALID_CURSORS.keys())
async def test_invalid_cursor_is_400(client, test_user, cursor):
    r = await client.get("/chats", params={"cursor": cursor}, headers=auth_headers(test_user))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Invalid cursor"


@pytest.mark.parametrize("limit,status", [(0, 422), (-1, 422), (201, 422), (1, 200), (200, 200)])
async def test_limit_bounds(client, test_user, limit, status):
    r = await client.get("/chats", params={"limit": limit}, headers=auth_headers(test_user))
    assert r.status_code == status, r.text


async def test_cursor_without_limit_uses_default_page_size(client, db, test_user):
    chats = [
        await _chat(db, test_user, BASE + timedelta(seconds=i))
        for i in range(CHATS_PAGE_DEFAULT + 3)
    ]
    h = auth_headers(test_user)
    r1 = await client.get("/chats", params={"limit": 1}, headers=h)
    r2 = await client.get("/chats", params={"cursor": r1.headers["x-next-cursor"]}, headers=h)
    assert len(r2.json()) == CHATS_PAGE_DEFAULT
    assert r2.headers.get("x-next-cursor")
    assert [r1.json()[0]["id"]] + [c["id"] for c in r2.json()] == _expected(chats)[
        : CHATS_PAGE_DEFAULT + 1
    ]


async def test_only_the_callers_chats_admin_included(client, db, test_user, admin_user):
    mine = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(3)]
    theirs = [await _chat(db, admin_user, BASE + timedelta(seconds=i)) for i in range(3)]

    for user, own in ((test_user, mine), (admin_user, theirs)):
        ids, _ = await _all_pages(client, user, {"limit": 2})
        assert ids == _expected(own)
        r = await client.get("/chats", headers=auth_headers(user))
        assert [c["id"] for c in r.json()] == _expected(own)
        assert {c["user_email"] for c in r.json()} == {user.email}

    # Another user's cursor reveals nothing: it only bounds the caller's own chats.
    cursor = encode_chat_cursor(BASE + timedelta(days=1), uuid.uuid4())
    r = await client.get("/chats", params={"cursor": cursor}, headers=auth_headers(test_user))
    assert [c["id"] for c in r.json()] == _expected(mine)


async def test_no_params_returns_every_chat_unpaginated(client, db, test_user):
    """Old callers send no limit or cursor and must keep getting the whole list."""
    n = 205  # more than the largest page
    db.add_all(
        Chat(
            user_id=test_user.id,
            title=f"c{i}",
            agent_namespace="ns",
            agent_name="bot",
            updated_at=BASE + timedelta(seconds=i % 50),
        )
        for i in range(n)
    )
    await db.flush()
    archived = await _chat(db, test_user, BASE, archived=True)
    h = auth_headers(test_user)

    r = await client.get("/chats", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) == n
    assert "x-next-cursor" not in r.headers
    rows = (
        await db.execute(select(Chat).where(Chat.user_id == test_user.id, Chat.archived.is_(False)))
    ).scalars()
    assert [c["id"] for c in body] == _expected(list(rows))
    assert set(body[0]) >= {
        "id",
        "user_id",
        "user_email",
        "agent_id",
        "agent_namespace",
        "agent_name",
        "title",
        "archived",
        "expires_at",
        "keep_alive",
        "active_channel_id",
        "created_at",
        "updated_at",
        "last_message_at",
    }

    r = await client.get("/chats", params={"include_archived": "true"}, headers=h)
    assert len(r.json()) == n + 1 and str(archived.id) in {c["id"] for c in r.json()}


async def test_last_message_at(client, db, test_user):
    busy = await _chat(db, test_user, BASE + timedelta(seconds=2))
    quiet = await _chat(db, test_user, BASE + timedelta(seconds=1))
    other = await _chat(db, test_user, BASE)
    latest = BASE + timedelta(hours=5)
    # Inserted out of order: the latest is neither the first nor the last row.
    for offset in (timedelta(hours=1), timedelta(hours=5), timedelta(hours=3)):
        db.add(Message(chat_id=busy.id, role="user", content="x", created_at=BASE + offset))
    db.add(Message(chat_id=other.id, role="user", content="x", created_at=BASE + timedelta(days=2)))
    await db.flush()
    h = auth_headers(test_user)

    for params in ({}, {"limit": 1}):
        r = await client.get("/chats", params=params, headers=h)
        by_id = {c["id"]: c["last_message_at"] for c in r.json()}
        assert datetime.fromisoformat(by_id[str(busy.id)]) == latest
        if not params:
            assert by_id[str(quiet.id)] is None
            assert datetime.fromisoformat(by_id[str(other.id)]) == BASE + timedelta(days=2)

    ids, _ = await _all_pages(client, test_user, {"limit": 1})
    assert ids == [str(busy.id), str(quiet.id), str(other.id)]


async def test_cursor_headers_exposed_to_browsers(client, test_user):
    r = await client.get(
        "/chats",
        params={"limit": 1},
        headers={**auth_headers(test_user), "Origin": "http://console.test"},
    )
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-next-cursor", "link", "x-truncated"} <= exposed


async def test_cursor_in_another_timezone_is_the_same_point(client, db, test_user):
    chats = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(4)]
    after = sorted(chats, key=lambda c: (c.updated_at, c.id), reverse=True)[1]
    plus_two = after.updated_at.astimezone(timezone(timedelta(hours=2)))
    r = await client.get(
        "/chats",
        params={"cursor": encode_chat_cursor(plus_two, after.id)},
        headers=auth_headers(test_user),
    )
    assert r.status_code == 200, r.text
    assert [c["id"] for c in r.json()] == _expected(chats)[2:]


async def test_unpaginated_past_the_cap_is_truncated_with_a_cursor(
    client, db, test_user, monkeypatch, caplog
):
    """A user with more chats than CHATS_UNPAGINATED_MAX gets the newest ones,
    told so, with the cursor for the rest (one response of every chat could
    take the backend's memory)."""
    monkeypatch.setattr(settings, "chats_unpaginated_max", 5)
    chats = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(8)]
    h = auth_headers(test_user)

    with caplog.at_level("WARNING", logger="app.api.runtime.endpoints.chats"):
        r = await client.get("/chats", headers=h)
    assert r.status_code == 200
    assert [c["id"] for c in r.json()] == _expected(chats)[:5]
    assert r.headers["x-truncated"] == "true"
    cursor = r.headers["x-next-cursor"]
    link = r.headers["link"]
    assert link.startswith("</chats?") and link.endswith('>; rel="next"')
    assert f"cursor={cursor}" in link and f"limit={CHATS_PAGE_MAX}" in link
    assert any(
        "more than 5 chats" in rec.getMessage() and str(test_user.id) in rec.getMessage()
        for rec in caplog.records
    )

    # The cursor continues where the truncated list stopped, as a page.
    r2 = await client.get("/chats", params={"cursor": cursor}, headers=h)
    assert [c["id"] for c in r2.json()] == _expected(chats)[5:]
    assert "x-next-cursor" not in r2.headers and "x-truncated" not in r2.headers

    # Filters carry over into the Link.
    r3 = await client.get("/chats", params={"include_archived": "true"}, headers=h)
    assert "include_archived=true" in r3.headers["link"]


@pytest.mark.parametrize("n", [4, 5])
async def test_unpaginated_up_to_the_cap_is_unchanged(client, db, test_user, monkeypatch, n):
    monkeypatch.setattr(settings, "chats_unpaginated_max", 5)
    chats = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(n)]
    r = await client.get("/chats", headers=auth_headers(test_user))
    assert [c["id"] for c in r.json()] == _expected(chats)
    for header in ("x-truncated", "x-next-cursor", "link"):
        assert header not in r.headers


async def test_cap_does_not_apply_to_pages(client, db, test_user, monkeypatch):
    monkeypatch.setattr(settings, "chats_unpaginated_max", 2)
    chats = [await _chat(db, test_user, BASE + timedelta(seconds=i)) for i in range(6)]
    r = await client.get("/chats", params={"limit": 4}, headers=auth_headers(test_user))
    assert [c["id"] for c in r.json()] == _expected(chats)[:4]
    assert "x-truncated" not in r.headers
    assert r.headers["x-next-cursor"]


@pytest.mark.parametrize("value", ["0", "-1"])
def test_cap_must_be_positive(monkeypatch, value):
    monkeypatch.setenv("CHATS_UNPAGINATED_MAX", value)
    with pytest.raises(ValueError, match="CHATS_UNPAGINATED_MAX"):
        Settings()
