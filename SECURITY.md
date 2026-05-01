# Security Policy

CATalyst is beta software that can control live trading wallets. Please treat
security reports as sensitive until a fix is available.

## Supported Versions

Security fixes are handled for the latest tagged release and the current default
branch, `main`.

## Automated Checks

Every push or pull request to `main` runs the normal quality gate:
syntax checks, crash-class Ruff linting, tests, a tracked-secret scan, Bandit,
and Python dependency auditing with `pip-audit`.

A separate deep security workflow runs Semgrep SAST and Gitleaks secret scanning
on pushes, pull requests, weekly schedules, and manual dispatches. Dependabot
alerts and automated security fixes are enabled for dependency updates.

## Reporting a Vulnerability

Please do not open a public issue for suspected vulnerabilities.

Use GitHub private vulnerability reporting when available:
https://github.com/Lowestofttim/catalyst-bot/security/advisories/new

If GitHub does not allow private reporting for your account, contact the
maintainer privately through the contact route listed on the GitHub profile.

Useful details include:

- CATalyst version or commit SHA
- Operating system and wallet type
- Whether the issue involves wallet access, local files, offer creation, or API
  exposure
- Exact steps to reproduce, with secrets removed
- Relevant log excerpts with wallet cert paths, tokens, seeds, and private data
  redacted

## Scope

In scope:

- Bugs that could expose wallet secrets, local auth tokens, cert paths, or
  private local data
- Bugs that could trigger unintended offer creation, cancellation, or trade
  execution
- Local API or desktop bridge behavior that can be reached by another process
  without clear operator consent
- Build or release-chain issues that could ship the wrong code or files

Out of scope:

- Market losses caused by normal trading risk or price movement
- Public blockchain data such as asset IDs, offer IDs, puzzle hashes, and
  confirmed transactions
- Reports that require malware already running as the same local user, unless
  CATalyst makes the impact materially worse

## Operator Safety

Never share seed phrases, private keys, wallet cert files, `.env`,
`user_secrets.json`, live database files, or full logs containing local secrets.
CATalyst maintainers should not ask for these.
