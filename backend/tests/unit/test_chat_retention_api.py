"""Chat retention: the admin API and its permissions, and the agent's
retentionDays through REST, config apply/export and change history."""

import uuid

import pytest
import yaml
from sqlalchemy import select

from app.core.config import settings
from app.models.agent import Agent
from app.models.config_revision import ConfigRevision
from app.models.retention_run import ChatRetentionRun
from tests.conftest import auth_headers

STATUS = "/api/v1/retention/status"
RUN = "/api/v1/retention/run"


@pytest.fixture(autouse=True)
def _retention(monkeypatch):
    monkeypatch.setattr(settings, "chat_retention_enabled", False)
    monkeypatch.setattr(settings, "chat_retention_days", 30)
    monkeypatch.setattr(settings, "chat_retention_action", "archive")
    monkeypatch.setattr(settings, "chat_archive_s3_bucket", None)


class TestPermissions:
    async def test_status_is_admin_only(self, client, test_user, admin_user):
        assert (await client.get(STATUS, headers=auth_headers(test_user))).status_code == 403
        r = await client.get(STATUS, headers=auth_headers(admin_user))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"]["enabled"] is False
        assert body["policy"]["default_days"] == 30
        assert body["destination"] == {"configured": False}
        assert "disabled" in body["refusal"]

    async def test_status_never_shows_credentials(self, client, admin_user, monkeypatch):
        monkeypatch.setattr(settings, "chat_archive_s3_bucket", "archive-bucket")
        monkeypatch.setattr(settings, "chat_archive_s3_access_key_id", "AKIAEXAMPLEKEYID")
        monkeypatch.setattr(settings, "chat_archive_s3_secret_access_key", "example-secret-value")
        r = await client.get(STATUS, headers=auth_headers(admin_user))
        assert r.status_code == 200
        assert "AKIAEXAMPLEKEYID" not in r.text and "example-secret-value" not in r.text
        assert r.json()["destination"]["credentials"] == "static keys"

    async def test_dry_run_needs_read_and_changes_nothing(self, client, db, test_user, admin_user):
        assert (await client.post(f"{RUN}?dry_run=true", headers=auth_headers(test_user))).status_code == 403
        r = await client.post(RUN, headers=auth_headers(admin_user))  # dry_run defaults to true
        assert r.status_code == 200, r.text
        assert r.json()["dry_run"] is True
        assert (await db.execute(select(ChatRetentionRun))).scalars().all() == []

    async def test_real_run_needs_update(self, client, test_user):
        r = await client.post(f"{RUN}?dry_run=false", headers=auth_headers(test_user))
        assert r.status_code == 403

    async def test_real_run_refused_while_disabled(self, client, db, admin_user):
        r = await client.post(f"{RUN}?dry_run=false", headers=auth_headers(admin_user))
        assert r.status_code == 409
        assert "disabled" in r.json()["detail"]
        assert (await db.execute(select(ChatRetentionRun))).scalars().all() == []

    async def test_real_run_is_queued_and_recorded(self, client, db, admin_user, monkeypatch):
        monkeypatch.setattr(settings, "chat_retention_enabled", True)
        monkeypatch.setattr(settings, "chat_retention_action", "delete")
        queued = []

        class Pool:
            async def enqueue_job(self, name, **kwargs):
                queued.append((name, kwargs))

        async def pool():
            return Pool()

        monkeypatch.setattr("app.core.redis.get_arq_pool", pool)
        r = await client.post(f"{RUN}?dry_run=false", headers=auth_headers(admin_user))
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert queued[0][0] == "chat_retention_run_job"
        assert queued[0][1]["run_id"] == run_id
        assert queued[0][1]["_queue_name"] == "sinas:queue:agents"
        run = await db.get(ChatRetentionRun, uuid.UUID(run_id))
        assert run.status == "queued" and run.trigger == "api"
        assert run.actor_user_id == admin_user.id
        # A second one while it is queued is refused.
        again = await client.post(f"{RUN}?dry_run=false", headers=auth_headers(admin_user))
        assert again.status_code == 409


def _agent_payload(**extra):
    return {"namespace": "rettest", "name": f"agent{uuid.uuid4().hex[:6]}", **extra}


class TestAgentField:
    async def test_setting_it_takes_the_retention_permission(self, client, test_user, admin_user):
        r = await client.post("/api/v1/agents", json=_agent_payload(retention_days=7), headers=auth_headers(test_user))
        assert r.status_code == 403
        r = await client.post("/api/v1/agents", json=_agent_payload(), headers=auth_headers(test_user))
        assert r.status_code == 201, r.text
        assert r.json()["retention_days"] is None
        path = f"/api/v1/agents/rettest/{r.json()['name']}"
        # The owner can edit the agent, but not its retention.
        r = await client.put(path, json={"retention_days": 7}, headers=auth_headers(test_user))
        assert r.status_code == 403
        # Sending the stored value (the console sends the whole form) is fine.
        r = await client.put(path, json={"retention_days": None, "description": "x"}, headers=auth_headers(test_user))
        assert r.status_code == 200, r.text
        r = await client.put(path, json={"retention_days": 7}, headers=auth_headers(admin_user))
        assert r.status_code == 200 and r.json()["retention_days"] == 7

    async def test_null_clears_left_out_keeps_zero_is_never(self, client, admin_user):
        r = await client.post("/api/v1/agents", json=_agent_payload(retention_days=14), headers=auth_headers(admin_user))
        assert r.status_code == 201, r.text
        path = f"/api/v1/agents/rettest/{r.json()['name']}"
        r = await client.put(path, json={"description": "other"}, headers=auth_headers(admin_user))
        assert r.json()["retention_days"] == 14
        r = await client.put(path, json={"retention_days": 0}, headers=auth_headers(admin_user))
        assert r.json()["retention_days"] == 0
        r = await client.put(path, json={"retention_days": None}, headers=auth_headers(admin_user))
        assert r.json()["retention_days"] is None
        r = await client.put(path, json={"retention_days": -1}, headers=auth_headers(admin_user))
        assert r.status_code == 422

    async def test_changes_are_in_the_history(self, client, db, admin_user):
        r = await client.post("/api/v1/agents", json=_agent_payload(), headers=auth_headers(admin_user))
        name = r.json()["name"]
        await client.put(f"/api/v1/agents/rettest/{name}", json={"retention_days": 21}, headers=auth_headers(admin_user))
        revisions = (
            await db.execute(
                select(ConfigRevision)
                .where(ConfigRevision.resource_kind == "agents", ConfigRevision.resource_key == f"rettest/{name}")
                .order_by(ConfigRevision.id)
            )
        ).scalars().all()
        assert revisions[-1].changes["retention_days"] == {"from": None, "to": 21}
        assert revisions[-1].spec["retention_days"] == 21


class TestConfigApplyExport:
    async def _apply(self, db, owner_id, agents):
        from app.schemas.config import SinasConfig
        from app.services.config_apply.service import ConfigApplyService

        config = SinasConfig.model_validate({
            "apiVersion": "sinas.co/v1", "kind": "SinasConfig", "metadata": {"name": "rettest"},
            "spec": {"agents": agents},
        })
        result = await ConfigApplyService(
            db, "rettest", owner_user_id=str(owner_id), managed_by="config", auto_commit=False
        ).apply_config(config)
        assert result.success, result.errors
        return result

    async def test_round_trip(self, db, admin_user):
        from app.services.config_export import ConfigExportService

        a, b, c = (f"cfg{uuid.uuid4().hex[:6]}" for _ in range(3))
        await self._apply(db, admin_user.id, [
            {"namespace": "rettest", "name": a, "retentionDays": "never"},
            {"namespace": "rettest", "name": b, "retentionDays": 21},
            {"namespace": "rettest", "name": c},
        ])
        rows = {
            ag.name: ag for ag in (
                await db.execute(select(Agent).where(Agent.namespace == "rettest"))
            ).scalars()
        }
        assert (rows[a].retention_days, rows[b].retention_days, rows[c].retention_days) == (0, 21, None)

        exported = yaml.safe_load(await ConfigExportService(db, managed_only=True, managed_by="config").export_config())
        by_name = {ag["name"]: ag for ag in exported["spec"]["agents"] if ag.get("namespace") == "rettest"}
        assert by_name[a]["retentionDays"] == 0
        assert by_name[b]["retentionDays"] == 21
        assert "retentionDays" not in by_name[c]

        # Re-applying the export changes nothing.
        result = await self._apply(db, admin_user.id, [by_name[a], by_name[b], by_name[c]])
        assert not result.summary.updated, result.summary

        # Leaving it out of config puts the agent back on the default.
        await self._apply(db, admin_user.id, [{"namespace": "rettest", "name": b}])
        await db.refresh(rows[b])
        assert rows[b].retention_days is None

    async def test_invalid_value_is_an_error(self, db, admin_user):
        from pydantic import ValidationError

        from app.schemas.config import SinasConfig

        with pytest.raises(ValidationError):
            SinasConfig.model_validate({
                "apiVersion": "sinas.co/v1", "kind": "SinasConfig", "metadata": {"name": "x"},
                "spec": {"agents": [{"namespace": "rettest", "name": "bad", "retentionDays": "soon"}]},
            })
