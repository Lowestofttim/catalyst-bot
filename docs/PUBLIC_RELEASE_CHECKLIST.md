# Public Release Checklist

This checklist is for making the GitHub repository public in a controlled way.
It is written for a non-developer maintainer.

## Current Repo State To Fix Before Going Public

- GitHub's default branch is currently `test`, but the cleaned and tested code
  is on `master`. Set the default branch to `master` before publishing, or
  visitors will see stale files.
- Branch protection cannot currently be enabled on this private repo without a
  paid plan. Once the repo is public, enable protection for `master`.
- Keep `.env`, wallet certs, database files, superlogs, and local scratch files
  out of git. The `.gitignore` already covers the important patterns.

## Before Switching Visibility To Public

- Confirm the working branch is clean and all intended changes are committed.
- Run the unit suite.
- Run the security scan.
- Run `python scripts/check_tracked_secrets.py`.
- Check `git status --ignored` for any surprising local files.
- Confirm no live database, wallet cert, token, or `.env` content is tracked.
- Review tracked planning documents, especially `docs/partial_offers/*.docx`,
  and remove or rewrite anything that is internal-only before switching the
  repository public.
- Confirm the latest release artifacts were built from the intended tag.
- Review README wording so users understand the beta and trading-risk status.

## GitHub Settings

- Default branch: `master`
- Issues: enabled
- Discussions: enabled
- Wiki: disabled unless you decide to maintain it
- Automatically delete head branches after merge: enabled
- Dependabot alerts: enabled
- Dependabot updates: enabled through `.github/dependabot.yml`
- GitHub Actions workflow permissions: read-only by default

## Branch Protection For `master`

Enable these when GitHub allows it:

- Require pull requests before merging
- Require status checks to pass
- Require the main unit test and security jobs
- Block force pushes
- Block branch deletion
- Require branches to be up to date before merge if the queue gets busy

The repo already includes `scripts/apply_branch_protection.sh`, which can apply
the intended protection once the repository is public or on a plan that supports
private branch protection.

## Release Flow

1. Work on a feature or fix branch.
2. Open a pull request into `master`.
3. Wait for tests and security checks to pass.
4. Merge only reviewed changes.
5. Tag a release such as `v1.2.2`.
6. Let GitHub Actions build the release artifacts.
7. Download and smoke-test the Windows installer before announcing it.

## What Not To Publish

- Wallet seed phrases or private keys
- Sage certificate and key files
- `.env` or `.env.*`
- `user_secrets.json`
- Live `*.db`, `*.sqlite`, `*.db-wal`, or `*.db-shm` files
- Full superlogs unless they have been reviewed and redacted
- Local helper scripts with machine-specific paths or tokens
