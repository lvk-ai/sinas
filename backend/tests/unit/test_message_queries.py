"""One agent message, a fixed number of queries.

Preparing a message loaded the agent five times, each enabled function and
skill with its own query, and the provider twice. On a database a few
milliseconds away, that was a noticeable part of the wait before the model
was even called. Now each is loaded once, whatever the number of tools.
"""
import re

from sqlalchemy import event

from app.core import database as core_db
from app.core.encryption import EncryptionService
from app.models import LLMProvider
from app.providers.openai_provider import OpenAIProvider
from app.services.message_service import MessageService
from tests.conftest import auth_headers


async def _statements_for_one_message(client, db, admin_user, monkeypatch, n_tools: int) -> list[str]:
    h = auth_headers(admin_user)
    ns = f"mq{n_tools}"
    provider = LLMProvider(name=f"p-{ns}", provider_type="openai", api_key="x", api_endpoint="http://x/v1",
                           default_model="m", is_active=True)
    db.add(provider)
    await db.flush()
    monkeypatch.setattr(EncryptionService, "decrypt", lambda self, v: "test-key")
    for i in range(n_tools):
        r = await client.post("/api/v1/functions", json={
            "namespace": ns, "name": f"f{i}", "code": "def handler(input, context):\n    return {}\n",
            "input_schema": {"type": "object", "properties": {}}, "output_schema": {"type": "object"},
        }, headers=h)
        assert r.status_code in (200, 201), r.text
        r = await client.post("/api/v1/skills", json={"namespace": ns, "name": f"s{i}", "description": "d", "content": "c"}, headers=h)
        assert r.status_code in (200, 201), r.text
    r = await client.post("/api/v1/agents", json={
        "namespace": ns, "name": "bot", "system_prompt": "Hi {{user.email}}", "llm_provider_id": str(provider.id),
        "enabled_functions": [f"{ns}/f{i}" for i in range(n_tools)],
        "enabled_skills": [{"skill": f"{ns}/s{i}", "preload": i == 0} for i in range(n_tools)],
    }, headers=h)
    assert r.status_code in (200, 201), r.text
    chat = (await client.post(f"/agents/{ns}/bot/chats", json={}, headers=h)).json()["id"]

    async def fake_stream(self, **_):
        yield {"content": "ok", "tool_calls": None, "finish_reason": "stop"}

    monkeypatch.setattr(OpenAIProvider, "stream", fake_stream)

    # The usage row is written in the background through the app's own
    # connection pool, which would keep a connection tied to this test's
    # event loop for the next test to trip over.
    async def no_usage(values):
        pass

    monkeypatch.setattr("app.providers.tracking._insert_usage", no_usage)
    svc = MessageService(db)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(" ".join(statement.split()))

    engines = [db.bind.sync_engine, core_db.async_engine.sync_engine]
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        async for _ in svc.send_message_stream(chat_id=chat, user_id=str(admin_user.id), user_token="", content="hi"):
            pass
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)
    return statements


def _from(statements: list[str], table: str) -> int:
    return sum(1 for s in statements if s.startswith("SELECT") and re.search(rf"\bFROM {table}\b", s))


async def test_each_thing_is_loaded_once_whatever_the_number_of_tools(client, db, admin_user, monkeypatch):
    few = await _statements_for_one_message(client, db, admin_user, monkeypatch, 2)
    many = await _statements_for_one_message(client, db, admin_user, monkeypatch, 8)
    reads = lambda statements: [s for s in statements if s.startswith("SELECT")]
    assert len(reads(many)) == len(reads(few)), (few, many)
    assert _from(many, "agents") == 1
    assert _from(many, "functions") == 1
    assert _from(many, "llm_providers") <= 1
