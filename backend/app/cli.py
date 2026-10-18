"""Operator commands, run inside the backend container.

    python -m app.cli setup-link [--email EMAIL]
    python -m app.cli retention [--dry-run | --run]
    python -m app.cli restore-chat <s3://bucket/key | file> [--dry-run] [...]

`setup-link` mints a one-time password setup link (default: for
SUPERADMIN_EMAIL) and prints the full console URL on stdout, alone, so a
script can capture it; everything else goes to stderr. Minting a link
revokes any earlier one for the same user.

`retention` prints, as JSON, what a chat retention run would archive and
delete per agent (the default, `--dry-run`), or runs one (`--run`).

`restore-chat` verifies a chat archive and writes the chat back (see
docs/design/chat-retention.md §5). An s3:// archive is read with the
standard AWS credential chain or `--profile`: credentials you provide for
reading, never Sinas's archive keys (which can only write).
"""
import argparse
import asyncio
import json
import sys
from datetime import UTC


async def _setup_link(email: str | None) -> int:
    from app.core.config import settings
    from app.core.database import AsyncSessionLocal, async_engine
    from app.services.setup_link import SetupLinkError, mint_setup_link

    # DEBUG turns on SQL echo, which writes to stdout: keep stdout the URL alone.
    async_engine.echo = False
    try:
        async with AsyncSessionLocal() as db:
            try:
                url, record, user = await mint_setup_link(db, email)
            except SetupLinkError as e:
                print(f"setup-link: {e}", file=sys.stderr)
                return 1
            has_password = bool(user.password_hash)
    finally:
        await async_engine.dispose()

    expires = record.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    what = "choose a new password" if has_password else "set a password and sign in"
    print(
        f"One-time link for {user.email} to {what}. Single use, valid until {expires} "
        f"({settings.password_reset_token_ttl_hours}h); any earlier link for this user "
        "no longer works:",
        file=sys.stderr,
    )
    print(url, flush=True)
    return 0


async def _retention(run: bool) -> int:
    from app.core.database import AsyncSessionLocal, async_engine
    from app.services.retention import job

    async_engine.echo = False
    try:
        if run:
            result = await job.run_retention(trigger="cli")
            print(json.dumps(result, indent=2))
            return 0 if result["status"] in ("completed", "partial") else 1
        async with AsyncSessionLocal() as db:
            print(json.dumps(await job.preview(db), indent=2))
        return 0
    finally:
        await async_engine.dispose()


async def _restore_chat(args) -> int:
    from app.core.database import AsyncSessionLocal, async_engine
    from app.services.retention.archive import InvalidArchiveError, parse_archive
    from app.services.retention.restore import apply_restore, plan_restore
    from app.services.retention.store import open_archive_source

    async_engine.echo = False
    try:
        data = await asyncio.to_thread(
            open_archive_source,
            args.source,
            endpoint_url=args.endpoint_url,
            region=args.region,
            profile=args.profile,
        )
    except Exception as e:
        print(f"restore-chat: could not read {args.source}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        archive = parse_archive(data)
    except InvalidArchiveError as e:
        print(f"restore-chat: invalid archive: {e}", file=sys.stderr)
        return 1

    try:
        async with AsyncSessionLocal() as db:
            plan = await plan_restore(
                db, archive, user_email=args.user_email, detach_session=args.detach_session
            )
            report = {"valid": True, "dry_run": args.dry_run, **plan.report()}
            if args.dry_run or not plan.ok:
                print(json.dumps(report, indent=2))
                return 0 if plan.ok else 1
            report["tool_results_restored"] = await apply_restore(db, archive, plan)
            await db.commit()
            report["restored"] = True
            print(json.dumps(report, indent=2))
            return 0
    finally:
        await async_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Sinas operator commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    link = sub.add_parser(
        "setup-link",
        help="Print a one-time password setup link (default: for SUPERADMIN_EMAIL).",
    )
    link.add_argument("--email", help="The user to mint the link for (default: SUPERADMIN_EMAIL).")

    retention = sub.add_parser(
        "retention", help="Chat retention: show what a run would do (default), or run one."
    )
    mode = retention.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report per agent; change nothing (default).")
    mode.add_argument("--run", action="store_true", help="Archive and delete now, as the schedule would.")

    restore = sub.add_parser("restore-chat", help="Verify a chat archive and restore the chat.")
    restore.add_argument("source", help="s3://bucket/key or a local .jsonl.gz file")
    restore.add_argument("--dry-run", action="store_true", help="Verify and report; write nothing.")
    restore.add_argument("--endpoint-url", help="S3-compatible endpoint (MinIO, GCS interop, R2).")
    restore.add_argument("--region", help="Region of the bucket.")
    restore.add_argument("--profile", help="AWS profile to read with (default: the standard chain).")
    restore.add_argument(
        "--user-email", help="Owner to restore to when the archived user no longer exists here."
    )
    restore.add_argument(
        "--detach-session",
        action="store_true",
        help="Restore without the chat's session key (done anyway when the agent's session "
        "continues in a newer chat).",
    )
    args = parser.parse_args(argv)

    if args.command == "setup-link":
        return asyncio.run(_setup_link(args.email))
    if args.command == "retention":
        return asyncio.run(_retention(run=args.run))
    if args.command == "restore-chat":
        return asyncio.run(_restore_chat(args))
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
