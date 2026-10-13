"""Schedules applier — the design's pilot kind (§5)."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import and_, select

from app.models.agent import Agent
from app.models.function import Function
from app.models.schedule import ScheduledJob
from app.schemas.spec.schedule import ScheduleSpec
from app.services.resources.base import (
    ApplyContext,
    ReferenceNotFound,
    ResourceApplier,
    SchedulerJobChanged,
)


class ScheduleApplier(ResourceApplier[ScheduleSpec]):
    kind = "schedules"
    label = "Schedule"
    noun = "schedule"
    config_section = "schedules"
    spec_model = ScheduleSpec
    model = ScheduledJob
    # is_active too: switching a schedule on needs its target active (see
    # check_new_references), which an earlier, disabled save didn't check.
    reference_fields = ("schedule_type", "target_namespace", "target_name", "is_active")
    _TARGET_FIELDS = ("schedule_type", "target_namespace", "target_name")

    _FIELDS = (
        "name", "schedule_type", "target_namespace", "target_name", "description",
        "cron_expression", "timezone", "input_data", "content", "is_active",
    )

    def key_of(self, spec: ScheduleSpec) -> str:
        return spec.name

    def key_of_row(self, row: ScheduledJob) -> str:
        return row.name

    def config_key(self, item: Any) -> str:
        return item.name

    async def find(self, ctx: ApplyContext, key: str) -> ScheduledJob | None:
        # Schedule names are globally unique (the column is UNIQUE), so the
        # lookup is global too; who may touch a row is the API layer's call.
        # Locked until commit, like every row an applier may write: two
        # concurrent applies must not both diff against the same old state.
        return (
            await ctx.db.execute(
                select(ScheduledJob)
                .where(ScheduledJob.name == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    def spec_from_row(self, row: ScheduledJob) -> ScheduleSpec:
        # No validation: legacy rows written by the config path before it
        # validated (bad cron, agent without content) must still be readable,
        # so they can be fixed, paused or deleted.
        return ScheduleSpec.model_construct(
            **{field: getattr(row, field) for field in self._FIELDS}
        )

    def new_row(self, spec: ScheduleSpec, ctx: ApplyContext) -> ScheduledJob:
        return ScheduledJob(user_id=uuid.UUID(str(ctx.owner_user_id)))

    def write_fields(self, row: ScheduledJob, spec: ScheduleSpec) -> None:
        for field in self._FIELDS:
            setattr(row, field, getattr(spec, field))

    async def check_new_references(
        self, spec: ScheduleSpec, ctx: ApplyContext, current: Optional[ScheduleSpec]
    ) -> None:
        """On create, and when the target changes: check it. When only
        is_active changed: switching on checks the target can run; pausing
        checks nothing, so a schedule whose target was deleted can still be
        paused."""
        target_changed = current is None or any(
            getattr(spec, f) != getattr(current, f) for f in self._TARGET_FIELDS
        )
        switched_on = current is not None and current.is_active is False and spec.is_active is not False
        if target_changed or switched_on:
            await self.check_references(spec, ctx)

    async def check_references(self, spec: ScheduleSpec, ctx: ApplyContext) -> None:
        """Same rules and messages the REST API always had, now on every
        channel. Config apply previously checked nothing here (it relied on a
        parser pre-pass that `force=true` skips), and a REST PATCH that changed
        the target to a pipeline never checked the pipeline at all."""
        ns, name, db = spec.target_namespace, spec.target_name, ctx.db
        # A schedule that fires needs a target that can run: functions and
        # pipelines must be active, agents need only exist. A disabled
        # schedule never fires, so it only needs its target to exist — a
        # package can ship a polling pipeline and its schedule both off, to
        # be switched on together. Turning the schedule on runs this check
        # again, with the target required active.
        need_active = spec.schedule_type != "agent" and spec.is_active is not False
        # In a preview the same config may be about to create the target.
        # Only in a preview: in a real apply it has been created by now, and
        # the database (not the config) is the truth — if creating it failed,
        # this schedule must fail too.
        kind = {"function": "functions", "agent": "agents", "pipeline": "pipelines"}
        if ctx.declared(kind[spec.schedule_type], spec.target, active=need_active):
            return
        if spec.schedule_type == "function":
            scope = ctx.reference_scope_user_id
            query = select(Function.id).where(Function.namespace == ns, Function.name == name)
            if scope:
                query = query.where(Function.user_id == uuid.UUID(str(scope)))
            if need_active:
                query = query.where(Function.is_active == True)  # noqa: E712
            if not (await db.execute(query)).first():
                raise ReferenceNotFound(f"Function '{ns}/{name}' not found")
        elif spec.schedule_type == "pipeline":
            from app.models import Pipeline

            query = select(Pipeline.id).where(Pipeline.namespace == ns, Pipeline.name == name)
            if need_active:
                query = query.where(Pipeline.is_active == True)  # noqa: E712
            if not (await db.execute(query)).first():
                raise ReferenceNotFound(
                    f"Pipeline '{ns}/{name}' not found or inactive" if need_active
                    else f"Pipeline '{ns}/{name}' not found"
                )
        else:
            found = (
                await db.execute(
                    select(Agent.id).where(and_(Agent.namespace == ns, Agent.name == name))
                )
            ).first()
            if not found:
                raise ReferenceNotFound(f"Agent '{ns}/{name}' not found")

    def effects(self, action: str, row: ScheduledJob) -> list[Any]:
        scheduler_action = {"create": "add", "update": "update", "delete": "remove"}[action]
        return [SchedulerJobChanged(scheduler_action, str(row.id))]
