#!/usr/bin/env bash
# Hourly confirmed progress (regulation 6.6) + layer-1 checks + push.
# Usage:  export CHECKPOINT_NAME=kuba CHECKPOINT_ZONE=backend/   (once, latin)
#         scripts/checkpoint.sh "[T3] what was done"
set -euo pipefail
MSG="${1:?Describe what was done: \"[T#] ...\"}"
WHO="${CHECKPOINT_NAME:-$(whoami)}"
MASTER="${MASTER_NAME:-abylay}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
TS="$(TZ=Asia/Almaty date '+%Y-%m-%d %H:%M')"
CHANGED="$( { git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u )"

# ---- layer 1: instant, offline ----
echo "$CHANGED" | grep -qxE '\.env(\..*)?' && { echo "✗ .env is in the changes — it must never be committed"; exit 1; }
git diff HEAD | grep -qE 'sk-[A-Za-z0-9_-]{20,}' && { echo "✗ something that looks like an API key is in the diff"; exit 1; }
if echo "$CHANGED" | grep -qx 'docs/CONTRACT.md' && [ "$WHO" != "$MASTER" ]; then
  echo "✗ docs/CONTRACT.md is changed by the master only — revert it or ask the master"; exit 1; fi
echo "$MSG" | grep -qE '^\[T[0-9]+\]' || echo "⚠ start the message with the task id, e.g. \"[T3] ...\" — the master's review needs it"
if [ -n "${CHECKPOINT_ZONE:-}" ]; then
  OUT="$(echo "$CHANGED" | grep -v "^${CHECKPOINT_ZONE}" | grep -vE '^docs/(progress|screenshots)/' || true)"
  [ -n "$OUT" ] && echo "⚠ outside your zone ($CHECKPOINT_ZONE): $(echo "$OUT" | tr '\n' ' ')"
fi

# ---- record + commit + push ----
mkdir -p docs/progress
echo "- $TS · $BRANCH · $MSG" >> "docs/progress/$WHO.md"
git add -A
git commit -qm "checkpoint($WHO): $MSG" || echo "nothing new to commit"
if git push -q -u origin "$BRANCH" 2>/dev/null; then echo "✓ $TS  $WHO: $MSG  (pushed)"
else echo "⚠ $TS  committed locally, PUSH FAILED — check network, then: git push -u origin $BRANCH"; exit 1; fi
