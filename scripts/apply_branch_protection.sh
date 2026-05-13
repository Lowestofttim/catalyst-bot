#!/usr/bin/env bash
# Apply branch protection rules to main.
#
# When to run:
#   - As soon as the repo is made public, OR
#   - When upgrading to GitHub Pro on a private repo.
#
# Branch protection is free on public repos; on private repos it
# requires Pro. GitHub returns 403 with "Upgrade to GitHub Pro or
# make this repository public" until one of those is true.
#
# What this enforces:
#
#   main (the release branch):
#     - Required status checks: lint-and-syntax, unit-tests, security-scan
#     - Linear history (no merge commits — squash/rebase only)
#     - No force pushes, no deletions
#     - PR review NOT required (solo dev workflow)
#     - Admin override available (enforce_admins=false), so the user
#       can still push version-bump commits at release time.
#
# Usage:
#   bash scripts/apply_branch_protection.sh

set -euo pipefail

REPO="catalystxch/catalyst-bot"

apply_protection() {
    local branch="$1"
    echo "Applying protection to '$branch'..."
    gh api "repos/${REPO}/branches/${branch}/protection" -X PUT --input - <<JSON
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["lint-and-syntax", "unit-tests", "security-scan"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": false,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": true
}
JSON
    echo "  OK: $branch"
}

apply_protection main

echo
echo "Branch protection applied. Verify at:"
echo "  https://github.com/${REPO}/settings/branches"
