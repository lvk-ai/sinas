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

## Pull requests

1. Fork the repo and create a topic branch from `dev` (the integration branch). Open your PR against `dev`, not `main`.
2. Make your change. Keep PRs focused — one logical change per PR.
3. Run the linters/tests before pushing:
   - Backend: `cd backend && ruff check . && black --check . && mypy . && pytest`
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
