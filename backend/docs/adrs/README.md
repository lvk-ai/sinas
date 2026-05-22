# Architectural Decision Records

Backend-scoped ADRs live in this directory. One file per decision.

## Naming

```
YYYY-MM-DD-kebab-case-title.md
```

- `YYYY-MM-DD` is the **proposal date** (when the doc is first drafted),
  not the date it lands or moves to "Accepted." That date is stable and
  doubles as a sort key.
- Title is lower-case, hyphen-separated, ≤ ~60 chars.

Example: `2026-05-13-job-scoped-jwts.md`

## Structure

Each ADR opens with a metadata block:

```markdown
# ADR: <Title>

- **Status:** Proposed | Accepted | Rejected | Superseded by <link>
- **Date:** YYYY-MM-DD              ← matches the filename
- **Authors:** Name(s)
- **Related code:** Bullet list of files / dirs the ADR concerns
```

Then sections in roughly this order (skip what doesn't apply):

1. **Context** — what's true today and why the status quo doesn't work.
2. **Decision** — what we're proposing or have decided.
3. **API / schema / interface sketch** — concrete enough to argue about.
4. **Impact** — table of components that change and how.
5. **Open questions** — for reviewers / future-us.
6. **What we'd NOT do** — explicitly out of scope for the first cut.
7. **Next steps** — sequenced if approved.

## Status transitions

- Update the `Status:` line in place; don't rename the file.
- When superseded, set `Status: Superseded by <new-ADR-filename>` and add
  a one-line note at the top of the doc pointing readers there.
