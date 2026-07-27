# Security Policy

## Supported versions

Security fixes are applied to the latest release and the current `main`
branch. Older releases may not receive backports.

## Reporting a vulnerability

Please use the repository's private security-advisory workflow. Do not open a
public issue for a suspected vulnerability and do not include live credentials,
message content, personal identifiers, database exports, screenshots, or
decryption material in a report.

Include the affected version, impact, minimal reproduction steps, and any
suggested mitigation. Maintainers will acknowledge a complete report as soon as
practical and coordinate disclosure after a fix is available.

## Deployment baseline

- Never expose development credentials or bind development services beyond
  loopback.
- Generate independent high-entropy administrator, session-signing, media, and
  connector secrets.
- Keep the Windows companion API authenticated even on loopback.
- Treat message databases, media, local queues, auth state, and runtime logs as
  sensitive data.
- Review all optional outbound integrations and allowlists before enabling
  them.
