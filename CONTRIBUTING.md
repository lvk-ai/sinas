# Contributing to Sinas

Thanks for your interest in contributing. This document covers the basics for submitting issues and pull requests.

## Before you start

By submitting a pull request, you agree to the [Contributor License Agreement](CLA.md).

For security issues, please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Reporting issues

Please use GitHub Issues. Include:
- What you expected to happen and what actually happened
- A minimal reproduction (config, commands, sample input)
- Your environment: Sinas version, OS, deployment method (Docker, local dev)
- Relevant logs (with secrets redacted)

## Development setup

See [INSTALL.md](INSTALL.md) for the full development environment setup. Quick version:

```bash
git clone https://github.com/sinas-platform/sinas.git && cd sinas
cp .env.example .env   # Fill in your keys and SMTP config
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

> **Note:** The Docker dev build pulls `@sinas/sdk` and `@sinas/ui` from npm automatically. If you want to run the console outside Docker (`cd console && npm install && npm run dev`), first install the published packages (`npm install @sinas/sdk @sinas/ui`) or `npm link` your local checkouts — the committed `file:` paths point at sibling repos that only exist in the maintainer's monorepo.

## Backend dependencies

`backend/pyproject.toml` declares the version **ranges** we support; `backend/uv.lock` pins the exact version and hash of every package, transitive ones included. The Docker image (`uv sync --locked`) and CI install from the lock only, so a rebuild gets the same packages as the last one instead of whatever was released that day. You need [uv](https://docs.astral.sh/uv/) (CI and the Dockerfile use 0.12.22).

- **Set up locally:** `cd backend && uv sync --extra dev` (creates `backend/.venv`; use `uv run <cmd>` or activate it).
- **Add a dependency or change a range:** edit `pyproject.toml`, run `uv lock`, commit both files.
- **Upgrade within the existing ranges:** `uv lock --upgrade-package <name>` (or `uv lock --upgrade` for everything), commit `uv.lock`. Bounded majors (`anthropic<2`, `sqlalchemy<2.1`, `mcp<3`) are only lifted by editing the range, after testing against the new major.
- **CI check:** the backend job runs `uv lock --check` and fails when `pyproject.toml` changed without a matching `uv lock`. The image build fails the same way (`--locked`), and uv refuses any downloaded file whose hash doesn't match the lock.

### Dependency updates

Dependabot (`.github/dependabot.yml`) opens one grouped PR per week, against `dev`, for each of: the backend lock (`uv.lock`), the console's npm deps and the GitHub Actions used in workflows. The backend group is `lockfile-only`: it moves packages within the pyproject ranges and never edits them, so a new major that needs a range change stays a deliberate PR. Review the grouped PR like any other: CI must be green; check the changelog of anything touching the LLM providers, the DB layer or auth.

Security updates are not grouped: Dependabot opens one PR per advisory (when "Dependabot security updates" is enabled in the repository settings). GitHub always targets those at the default branch (`main`), so bring the fix into `dev` as well.

## Pull requests

1. Fork the repo and create a topic branch from `dev` (the integration branch). Open your PR against `dev`, not `main`.
2. Make your change. Keep PRs focused — one logical change per PR.
3. Run the linters/tests before pushing:
   - Backend: `cd backend && uv run ruff check . && uv run black --check . && uv run mypy . && uv run pytest`
   - Console: `cd console && npm run lint && npm run typecheck`
4. Write a clear PR description: what changed, why, and how to verify.
5. Make sure CI is green.

## Coding standards

- **Python**: `black` formatting, `ruff` for linting, `mypy` for type checks. Config lives in `backend/pyproject.toml`.
- **TypeScript**: ESLint configured in `console/eslint.config.js`. Prefer strict types — avoid `any`.
- **Commits**: Use clear, imperative-mood commit messages ("Add foo", "Fix bar"). Reference issue numbers where relevant.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you are expected to uphold it.

## Questions

For usage questions, please open a GitHub Discussion rather than an issue.
