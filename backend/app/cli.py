"""Operator commands, run inside the backend container.

    python -m app.cli setup-link [--email EMAIL]

`setup-link` mints a one-time password setup link (default: for
SUPERADMIN_EMAIL) and prints the full console URL on stdout, alone, so a
script can capture it; everything else goes to stderr. Minting a link
revokes any earlier one for the same user.
"""
import argparse
import asyncio
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Sinas operator commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    link = sub.add_parser(
        "setup-link",
        help="Print a one-time password setup link (default: for SUPERADMIN_EMAIL).",
    )
    link.add_argument("--email", help="The user to mint the link for (default: SUPERADMIN_EMAIL).")
    args = parser.parse_args(argv)

    if args.command == "setup-link":
        return asyncio.run(_setup_link(args.email))
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
