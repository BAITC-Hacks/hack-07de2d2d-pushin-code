#!/usr/bin/env bash
# Fallback watcher (when the master is NOT running /loop in a Claude session):
# every INTERVAL seconds fetch, and headless-review every branch with a new, unreviewed commit.
# Usage: scripts/watch.sh      INTERVAL=300 REVIEWER=claude|codex
set -euo pipefail
INTERVAL="${INTERVAL:-300}"; mkdir -p docs/review
while true; do
  git fetch -q --prune origin
  for BR in $(git for-each-ref --format='%(refname:short)' refs/remotes/origin | sed 's|^origin/||' | grep -vE '^(HEAD|main)$'); do
    SHA="$(git rev-parse --short "origin/$BR")"
    ls docs/review 2>/dev/null | grep -q -- "-$SHA\.md$" && continue
    echo "[$(TZ=Asia/Almaty date +%H:%M)] new push: $BR@$SHA"
    scripts/review.sh "$BR" || echo "review failed: $BR"
  done
  sleep "$INTERVAL"
done
