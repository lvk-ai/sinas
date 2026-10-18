"""Chat retention: archive chats idle past their retention period to
write-only object storage, then delete them from Postgres.

- `policy`: settings, the per-agent override, the schedule
- `archive`: the archive format (build, parse, verify)
- `store`: the PutObject-only S3 writer
- `job`: selection, the per-chat archive-then-delete, runs, the dry run
- `restore`: the restore CLI

See docs/design/chat-retention.md.
"""
