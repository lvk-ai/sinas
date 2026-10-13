"""
Configuration apply service
Handles idempotent application of declarative configuration
"""
import hashlib
import json
import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.config import (
    ConfigApplyResponse,
    ConfigApplySummary,
    ResourceChange,
    SinasConfig,
)

from app.services.config_apply.identity import apply_users
from app.services.config_apply.data_sources import (
    apply_connection_annotations,
)
from pydantic.alias_generators import to_camel

from app.services.resources import ApplyContext, SideEffectBus

logger = logging.getLogger(__name__)


class ConfigApplyService:
    """Service for applying declarative configuration"""

    def __init__(
        self,
        db: AsyncSession,
        config_name: str,
        owner_user_id: str,
        managed_by: str = "config",
        auto_commit: bool = True,
        skip_resource_types: Optional[set[str]] = None,
        prune_missing: bool = False,
        supplied_secrets: Optional[set[str]] = None,
        adopt_unmanaged: bool = False,
    ):
        self.db = db
        # Package installs: take over resources made or edited by hand
        # instead of skipping them (see ownership_decision).
        self.adopt_unmanaged = adopt_unmanaged
        # Shared secrets whose value a package preview has but doesn't save
        # (secret variables): the dry run counts them as present.
        self.supplied_secrets = supplied_secrets or set()
        self.config_name = config_name
        self.owner_user_id = owner_user_id
        self.managed_by = managed_by
        self.auto_commit = auto_commit
        self.skip_resource_types = skip_resource_types or set()
        # Remove resources this source manages but no longer declares (package
        # upgrades). Only for kinds with an applier: their removals are
        # recorded in the change history, so they can be restored.
        self.prune_missing = prune_missing
        self.summary = ConfigApplySummary()
        self.changes: list[ResourceChange] = []
        # Post-commit notifications. Collected during apply and published only
        # after the transaction commits, so a worker can never observe an event
        # for a row it cannot read yet. When auto_commit is False the caller
        # owns the commit and must call flush_notifications() itself.
        # Effects from resources already migrated to the per-resource
        # appliers (docs/design/config-apply-unification.md); the two lists
        # below are the not-yet-migrated kinds and fold into this bus as they
        # move over.
        self.effects = SideEffectBus()
        self._pending_references: dict[str, dict[str, bool]] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []

        # Resource lookup caches (name -> id)
        self.user_ids: dict[str, str] = {}
        self.datasource_ids: dict[str, str] = {}
        self.webhook_ids: dict[str, str] = {}

    def _calculate_hash(self, data: dict[str, Any]) -> str:
        """Calculate hash for change detection"""
        # Create stable JSON string and hash it
        data_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(data_str.encode()).hexdigest()

    def _track_change(
        self,
        action: str,
        resource_type: str,
        resource_name: str,
        details: Optional[str] = None,
        changes: Optional[dict[str, Any]] = None,
        reason: Optional[str] = None,
    ):
        """Track a resource change"""
        self.changes.append(
            ResourceChange(
                action=action,
                resourceType=resource_type,
                resourceName=resource_name,
                details=details,
                changes=changes,
                reason=reason,
            )
        )

        # Update summary - map action to summary field name
        action_field_map = {
            "create": "created",
            "update": "updated",
            "unchanged": "unchanged",
            "skipped": "skipped",
            "delete": "deleted",
        }
        summary_field = action_field_map.get(action, action)
        summary_dict = getattr(self.summary, summary_field)
        summary_dict[resource_type] = summary_dict.get(resource_type, 0) + 1


    async def flush_notifications(self) -> None:
        """Publish queued events to the scheduler and CDC workers.

        Call AFTER the transaction commits. Callers that pass auto_commit=False
        (package install, for one) own their commit and must call this, or
        config-applied schedules never reach the running scheduler and CDC
        triggers are not picked up until a restart. Best-effort throughout: a
        notification failure must not fail an apply that already committed.
        """
        await self.effects.flush()

    async def apply_config(self, config: SinasConfig, dry_run: bool = False) -> ConfigApplyResponse:
        """
        Apply configuration idempotently

        Args:
            config: Validated configuration
            dry_run: If True, don't actually apply changes

        Returns:
            ConfigApplyResponse with results
        """
        self._pending_references = {
            kind: {
                f"{item.namespace}/{item.name}": getattr(item, "isActive", True) is not False
                for item in getattr(config.spec, kind)
            }
            for kind in ("functions", "agents", "pipelines")
        }
        # What agents may refer to: declared in the same apply counts (it's
        # applied all-or-nothing, and before the agents).
        for kind in ("skills", "queries", "stores", "collections", "components", "connectors", "mcpServers"):
            self._pending_references[kind] = {
                f"{item.namespace}/{item.name}": True for item in getattr(config.spec, kind, None) or []
            }
        # A function or agent the config leaves isActive unset on keeps its
        # current state, so a disabled one stays disabled: a preview must see
        # that, or it accepts a reference the real apply refuses. A
        # declaration that does set isActive (the same one listed again)
        # decides, as it does in the real apply.
        from sqlalchemy import or_, select

        from app.models.agent import Agent
        from app.models.function import Function
        from app.models.pipeline import Pipeline

        for kind, model in (("functions", Function), ("agents", Agent), ("pipelines", Pipeline)):
            items = getattr(config.spec, kind)
            unset = [i for i in items if i.isActive is None]
            if not unset:
                continue
            explicit = {f"{i.namespace}/{i.name}" for i in items if i.isActive is not None}
            disabled = (await self.db.execute(
                select(model.namespace, model.name).where(
                    model.is_active.is_(False),
                    or_(*((model.namespace == i.namespace) & (model.name == i.name) for i in unset)),
                )
            )).all()
            for namespace, name in disabled:
                if f"{namespace}/{name}" not in explicit:
                    self._pending_references[kind][f"{namespace}/{name}"] = False
        if "llmProviders" not in self.skip_resource_types:
            self._pending_references["llmProviders"] = {
                item.name: True for item in config.spec.llmProviders
            }
        self._pending_references["secrets"] = {name: True for name in self.supplied_secrets}
        if not dry_run:
            # Promotions to "the default" serialize on a lock taken before any
            # row lock this apply takes (lock order: see lock_singleton).
            from app.services.resources import ApplyContext
            from app.services.resources.base import lock_singleton
            from app.services.resources.registry import all_appliers

            for applier in all_appliers():
                if applier.singleton_lock and applier.kind not in self.skip_resource_types and any(
                    getattr(item, "isDefault", None) for item in getattr(config.spec, applier.config_section)
                ):
                    await lock_singleton(ApplyContext(db=self.db, origin="config"), applier.singleton_lock)
        # Packages skip connections: one declared there is never created.
        if "databaseConnections" not in self.skip_resource_types:
            self._pending_references["databaseConnections"] = {
                item.name: True for item in config.spec.databaseConnections
            }
        try:
            # Common kwargs shared by all appliers
            common = dict(
                db=self.db,
                dry_run=dry_run,
                managed_by=self.managed_by,
                config_name=self.config_name,
                calculate_hash=self._calculate_hash,
                track_change=self._track_change,
                errors=self.errors,
                warnings=self.warnings,
            )

            # Apply resources in dependency order
            from app.services.resources.registry import all_appliers

            # Roles first: users (still applied by the legacy path) hold them.
            role_applier = next(a for a in all_appliers() if a.kind == "roles")
            if "roles" not in self.skip_resource_types:
                await self._apply_kind(role_applier, config.spec.roles, dry_run)
            if "users" not in self.skip_resource_types:
                await apply_users(
                    **common,
                    users=config.spec.users,
                    user_ids=self.user_ids,
                )

            # Kinds with a per-resource applier: secrets, llmProviders, databaseConnections, dependencies, connectors, functions, agents, pipelines,
            # skills, queries, templates, collections, stores, manifests,
            # components, webhooks, schedules, databaseTriggers — after
            # everything they can point at. (Nothing checks a reference to a
            # connector, function, skill or query from agents or pipelines
            # yet; when those migrate, these move ahead of them.)
            from app.services.resources.registry import all_appliers

            for applier in all_appliers():
                if applier.kind not in self.skip_resource_types and applier.kind != "roles":
                    await self._apply_kind(
                        applier, getattr(config.spec, applier.config_section), dry_run
                    )
                if applier.kind == "databaseConnections" and applier.kind not in self.skip_resource_types:
                    # Table annotations: an additive layer on the connections
                    # just applied (never removed by config).
                    await apply_connection_annotations(
                        self.db, config.spec.databaseConnections, dry_run, self._track_change, self.errors
                    )

            if self.prune_missing:
                await self._prune_missing(config, dry_run)

            if self.errors:
                # All or nothing. A config or package with any resource that
                # fails changes nothing at all: it used to report success and
                # commit everything else, leaving a package "installed" without
                # the parts that failed. Callers that own the transaction
                # (auto_commit=False: package install) get success=False and
                # roll back themselves; a dry run reports the same verdict the
                # real apply would reach.
                self._discard_pending()
                if not dry_run and self.auto_commit:
                    await self.db.rollback()
                return ConfigApplyResponse(
                    success=False,
                    summary=self.summary,
                    changes=self.changes,
                    errors=self.errors,
                    warnings=self.warnings,
                )

            if not dry_run and self.auto_commit:
                await self.db.commit()
                await self.flush_notifications()

            return ConfigApplyResponse(
                success=True,
                summary=self.summary,
                changes=self.changes,
                errors=self.errors,
                warnings=self.warnings,
            )

        except Exception as e:
            logger.error(f"Error applying config: {str(e)}", exc_info=True)
            await self.db.rollback()
            self._discard_pending()  # nothing committed, so nothing to announce
            return ConfigApplyResponse(
                success=False,
                summary=self.summary,
                changes=self.changes,
                errors=[f"Fatal error: {str(e)}"],
                warnings=self.warnings,
            )

    # ------------------------------------------------------------------
    # Kinds migrated to per-resource appliers
    # ------------------------------------------------------------------

    async def _prune_missing(self, config: SinasConfig, dry_run: bool) -> None:
        """Delete what this source manages but no longer declares.

        Scoped to resources stamped with this source's managed_by — and, for a
        plain config (where every file shares managed_by="config"), to its
        config_name too, or applying one file would delete another's
        resources. A resource someone edited by hand was detached from the
        package at that edit, so an upgrade never removes it. Part of the same
        all-or-nothing transaction, reported in summary.deleted, and a dry run
        lists what would go without removing anything.
        """
        from sqlalchemy import select

        from app.services.resources.registry import all_appliers

        ctx = self._resource_context(dry_run)
        appliers = {a.kind: a for a in all_appliers()}

        # What this source no longer declares.
        candidates: list[tuple[Any, Any, str]] = []
        for applier in appliers.values():
            if applier.kind in self.skip_resource_types or not applier.deleted_with_package:
                continue
            declared = {
                applier.config_key(item)
                for item in getattr(config.spec, applier.config_section, None) or []
            }
            model = applier.model
            stmt = select(model).where(model.managed_by == self.managed_by)
            if not self.managed_by.startswith("pkg:"):
                stmt = stmt.where(model.config_name == self.config_name)
            if not dry_run:
                # As for uninstall: a row detached by a concurrent manual edit
                # drops out of the filter once locked, and is kept.
                stmt = stmt.with_for_update().execution_options(populate_existing=True)
            for row in (await self.db.execute(stmt)).scalars().all():
                key = applier.key_of_row(row)
                if key not in declared:
                    candidates.append((applier, row, key))
        if not candidates:
            return

        # Collections and stores holding what users put in them stay: a prune
        # removes definitions, and those contents couldn't be restored from
        # history. Deleting them stays a deliberate act. Decided first, since
        # what they keep using must stay too.
        removable = []
        for applier, row, key in candidates:
            contents = await _contents(self.db, applier.kind, row)
            if contents:
                self.warnings.append(
                    f"Kept {applier.label.lower()} '{key}' although no longer declared: it holds "
                    f"{contents}. Delete it in the console to remove it with its contents."
                )
            else:
                removable.append((applier, row, key))

        removing = {(applier.kind, key) for applier, _row, key in removable}
        users = await self._users_of(removing, config, appliers, ctx)

        for applier, row, key in removable:
            label = applier.label.lower()
            still_used = sorted(users.get((applier.kind, key), ()))
            if still_used:
                self.errors.append(
                    f"Can't remove {label} '{key}': still used by {', '.join(still_used)}. "
                    "Remove or change those too, or keep declaring it."
                )
                continue
            try:
                async with self.db.begin_nested():
                    await applier.delete(row, ctx)
            except Exception as e:
                self.errors.append(f"Error removing {label} '{key}': {_describe_error(e)}")
                continue
            self._track_change("delete", applier.kind, key)

    async def _users_of(self, removing: set, config: SinasConfig, appliers: dict, ctx) -> dict:
        """For each (kind, key) about to be removed, the resources that still
        point at it: rows that stay, and what this config declares (in a dry
        run those don't exist yet)."""
        from sqlalchemy import select

        from app.services.resources.usage import REFERRING_KINDS, references_of

        users: dict = {}
        for kind in REFERRING_KINDS:
            applier = appliers.get(kind)
            if applier is None:
                continue
            # The state after this apply: what's stored, with what the config
            # declares in its place (in a dry run it isn't written yet).
            rows = {
                applier.key_of_row(row): row
                for row in (await self.db.execute(select(applier.model))).scalars().all()
            }
            after = {key: await applier.current_spec(ctx, row) for key, row in rows.items()}
            for item in getattr(config.spec, applier.config_section, None) or []:
                try:
                    spec = applier.spec_model.model_validate(item.model_dump(exclude_none=True))
                except Exception:
                    continue  # reported by the apply itself
                key = applier.key_of(spec)
                existing = rows.get(key)
                if existing is not None and applier.ownership(existing, ctx) == "skip":
                    continue  # not this apply's to write: what's stored stays
                after[key] = spec
            for key, spec in after.items():
                if (kind, key) in removing:
                    continue  # going too
                for ref in references_of(kind, spec) & removing:
                    users.setdefault(ref, set()).add(f"{applier.noun} '{key}'")
        return users

    def _discard_pending(self) -> None:
        """Forget every queued notification: the transaction won't commit."""
        self.effects.discard()

    def _resource_context(self, dry_run: bool) -> ApplyContext:
        return ApplyContext(
            db=self.db,
            origin="package" if self.managed_by.startswith("pkg:") else "config",
            actor_user_id=self.owner_user_id,
            owner_user_id=self.owner_user_id,
            managed_by=self.managed_by,
            config_name=self.config_name,
            dry_run=dry_run,
            effects=self.effects,
            pending_references=self._pending_references,
            adopt_unmanaged=self.adopt_unmanaged,
        )

    async def _apply_kind(self, applier, items: list, dry_run: bool) -> None:
        ctx = self._resource_context(dry_run)
        for item in items:
            key = applier.config_key(item)
            try:
                declared = item.model_dump(exclude_none=True)
                spec = applier.spec_model.model_validate(declared)
                keep = {
                    field for field in applier.keep_unless_declared
                    if to_camel(field) not in declared
                }
                # One savepoint per resource: a failing one is reported and
                # rolled back on its own instead of poisoning the session for
                # every resource after it.
                async with self.db.begin_nested():
                    result = await applier.apply(spec, ctx, keep=keep)
            except Exception as e:
                self.errors.append(f"Error applying {applier.noun} '{key}': {_describe_error(e)}")
                continue
            if result.warning:
                self.warnings.append(result.warning)
            self._track_change(
                result.action,
                applier.kind,
                key,
                changes=result.changes or None,
                reason=result.reason,
            )


def _describe_error(error: Exception) -> str:
    """One line per problem, rather than pydantic's multi-line dump."""
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'spec'}: {err['msg']}"
            for err in error.errors(include_url=False)
        )
    return str(error)


async def _contents(db, kind: str, row) -> str | None:
    """What users stored in a collection or store, if anything."""
    from sqlalchemy import func, select

    if kind == "collections":
        from app.models.file import File

        count = (await db.execute(select(func.count()).select_from(File).where(File.collection_id == row.id))).scalar()
        return f"{count} file{'s' if count != 1 else ''}" if count else None
    if kind == "stores":
        from app.models.state import State

        count = (await db.execute(select(func.count()).select_from(State).where(State.store_id == row.id))).scalar()
        return f"{count} saved state{'s' if count != 1 else ''}" if count else None
    return None
