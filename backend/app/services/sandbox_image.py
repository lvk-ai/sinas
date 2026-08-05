"""Baked sandbox image: build & resolve the image untrusted code runs in.

See ADR 2026-06-17-ephemeral-sandbox-baked-image.md.

The image tag is a pure function of `hash(base_image + sorted dependency specs)`,
derivable from the `Dependency` table (the source of truth). Redis caches the
currently-built tag and holds a build lock; it is a cache, not authoritative
state — on a miss any process recomputes the tag from the deps and rebuilds.

Docker SDK calls are blocking, so they run via `asyncio.to_thread`. The SDK is
imported lazily so this module is importable in socket-free contexts.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import shlex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.models.dependency import Dependency

logger = logging.getLogger(__name__)

REDIS_TAG_KEY = "sinas:sandbox:image_tag"  # current built+ready tag
REDIS_BUILD_LOCK = "sinas:sandbox:build_lock"  # build mutex
_IMAGE_REPO = "sinas-sandbox"
_BUILD_LOCK_TTL = 600  # seconds; an upper bound on a single image build
_BUILD_WAIT_TIMEOUT = 600  # how long a non-builder waits for the builder


def _pip_specs(deps: list[Dependency]) -> list[str]:
    """Render Dependency rows to sorted pip requirement strings.

    Mirrors the spec logic in `container_pool._install_packages` so the baked
    image installs exactly what the pool would have installed.
    """
    specs: list[str] = []
    for pkg in deps:
        version = pkg.version
        if version:
            if any(version.startswith(op) for op in (">=", "<=", "~=", "!=", ">", "<")):
                specs.append(f"{pkg.package_name}{version}")
            else:
                specs.append(f"{pkg.package_name}=={version}")
        else:
            specs.append(pkg.package_name)
    return sorted(specs)


async def _dependency_specs(db: AsyncSession) -> list[str]:
    result = await db.execute(select(Dependency))
    return _pip_specs(list(result.scalars().all()))


def _tag_for(base_image: str, specs: list[str]) -> str:
    """Deterministic tag from the base image + the (sorted) dependency set."""
    h = hashlib.sha256()
    h.update(base_image.encode())
    h.update(b"\x00")
    h.update("\n".join(specs).encode())
    return f"{_IMAGE_REPO}:{h.hexdigest()[:16]}"


def _render_dockerfile(base_image: str, specs: list[str]) -> bytes:
    lines = [f"FROM {base_image}"]
    if specs:
        joined = " ".join(shlex.quote(s) for s in specs)
        lines.append(f"RUN pip install --no-cache-dir {joined}")
    return ("\n".join(lines) + "\n").encode()


async def desired_tag(db: AsyncSession) -> str:
    """The tag the current dependency set *should* resolve to (built or not)."""
    return _tag_for(settings.function_container_image, await _dependency_specs(db))


async def current_sandbox_image(db: AsyncSession) -> str:
    """Tag of a ready-to-run baked sandbox image.

    Self-correcting: compares the cached tag against the tag the *current*
    dependency set should resolve to, and rebuilds on a miss or mismatch. This
    means correctness does not depend on every dependency-mutation site
    remembering to trigger a rebuild — a changed dependency set is detected here.
    Normally the cache is warm (built proactively on dependency change), so this
    is a cheap Redis GET + one indexed query on the hot path.
    """
    desired = await desired_tag(db)
    redis = await get_redis()
    cached = await redis.get(REDIS_TAG_KEY)
    if cached == desired:
        return cached
    # Cold cache or the dependency set changed since the last build. Build the
    # image for the current deps (idempotent: build_sandbox_image skips the
    # build if the image already exists and just repoints the cache).
    logger.info("Sandbox image not current (cached=%s desired=%s) — building", cached, desired)
    return await build_sandbox_image(db)


def _image_exists(client, tag: str) -> bool:
    import docker

    try:
        client.images.get(tag)
        return True
    except docker.errors.ImageNotFound:
        return False


def _build_image(client, tag: str, base_image: str, specs: list[str]) -> None:
    dockerfile = _render_dockerfile(base_image, specs)
    logger.info("Building sandbox image %s (%d packages)", tag, len(specs))
    client.images.build(fileobj=io.BytesIO(dockerfile), tag=tag, pull=False, rm=True)


def _prune_old_images(client, keep_tag: str) -> None:
    """Best-effort removal of superseded sinas-sandbox images."""
    try:
        for img in client.images.list(name=_IMAGE_REPO):
            for t in list(img.tags):
                if t.startswith(f"{_IMAGE_REPO}:") and t != keep_tag:
                    try:
                        client.images.remove(t, force=False, noprune=False)
                    except Exception as e:  # in-use or shared layer — leave it
                        logger.debug("Skip pruning %s: %s", t, e)
    except Exception as e:
        logger.debug("Image prune skipped: %s", e)


async def build_sandbox_image(db: AsyncSession) -> str:
    """Ensure the baked image for the current dependency set exists, then point
    the Redis cache at it. Idempotent and concurrency-safe via a Redis lock.
    Returns the tag.
    """
    import docker

    base_image = settings.function_container_image
    specs = await _dependency_specs(db)
    tag = _tag_for(base_image, specs)
    redis = await get_redis()
    client = await asyncio.to_thread(docker.from_env)

    # Already built? Just (re)point the cache and return.
    if await asyncio.to_thread(_image_exists, client, tag):
        await redis.set(REDIS_TAG_KEY, tag)
        return tag

    got_lock = await redis.set(REDIS_BUILD_LOCK, tag, nx=True, ex=_BUILD_LOCK_TTL)
    if not got_lock:
        # Another process is building. Wait for the image to appear; fall back
        # to building ourselves (idempotent by tag) if it never does.
        waited = 0.0
        while waited < _BUILD_WAIT_TIMEOUT:
            if await asyncio.to_thread(_image_exists, client, tag):
                await redis.set(REDIS_TAG_KEY, tag)
                return tag
            await asyncio.sleep(1.0)
            waited += 1.0
        logger.warning("Build lock holder did not finish in %ss; building anyway", _BUILD_WAIT_TIMEOUT)

    try:
        if not await asyncio.to_thread(_image_exists, client, tag):
            await asyncio.to_thread(_build_image, client, tag, base_image, specs)
        # Atomic flip: only point the cache once the image is present.
        await redis.set(REDIS_TAG_KEY, tag)
        await asyncio.to_thread(_prune_old_images, client, tag)
        logger.info("Sandbox image ready: %s", tag)
        return tag
    finally:
        if got_lock:
            await redis.delete(REDIS_BUILD_LOCK)
