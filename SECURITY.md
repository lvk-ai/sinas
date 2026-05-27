# Security Policy

## Reporting a Vulnerability

If you discover a security issue in Sinas, please report it privately so we can address it before public disclosure.

**Email:** hello@sinas.co

Please include:
- A description of the vulnerability and its impact
- Steps to reproduce (proof of concept welcome)
- The affected version or commit SHA
- Your contact information for follow-up

We will acknowledge receipt within 2 business days and provide an estimated timeline for a fix. We ask that you give us a reasonable window to resolve the issue before publishing details.

## Supported Versions

Security fixes are applied to the latest released version on `main`. Older versions are not patched.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest | :x:               |

## Scope

In scope:
- The Sinas backend (`backend/`)
- The Sinas console (`console/`)
- Default Docker deployment as documented in `INSTALL.md`

Out of scope:
- Third-party dependencies (please report upstream)
- Self-hosted deployments where users have modified the default configuration in ways that weaken security
- Vulnerabilities requiring physical access or pre-existing admin credentials

## Disclosure

Once a fix is available, we credit reporters in the release notes unless they prefer to remain anonymous.
