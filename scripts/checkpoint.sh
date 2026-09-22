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
SECRET_RE='sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|-----BEGIN [A-Z ]*PRIVATE KEY-----'

# Stage everything FIRST, then inspect the staged diff. Inspecting `git diff HEAD`
# misses newly added files entirely — which is exactly where a leaked key usually is.
git add -A
CHANGED="$(git diff --cached --name-only)"
BLOCK=""

if echo "$CHANGED" | grep -qxE '\.env(\..*)?'; then
  BLOCK="$BLOCK
✗ .env is staged — it must never be committed"
fi

if git diff --cached | grep -qE "$SECRET_RE"; then
  BLOCK="$BLOCK
✗ something that looks like a secret is in the staged changes:
$(git diff --cached | grep -oE "$SECRET_RE" | sort -u | head -3 | sed 's/^/    /')
  files: $(git diff --cached --name-only -G"$SECRET_RE" | tr '\n' ' ')"
fi

if echo "$CHANGED" | grep -qx 'docs/CONTRACT.md' && [ "$WHO" != "$MASTER" ]; then
  BLOCK="$BLOCK
✗ docs/CONTRACT.md is changed by the master only — revert it or ask the master"
fi

if [ -n "$BLOCK" ]; then
  printf '%s\n' "$BLOCK"
  git reset -q
  echo "nothing was committed, nothing was pushed — fix the above and run again"
  exit 1
fi

# ---- warnings: never block, the venue network is unreliable enough as it is ----
echo "$MSG" | grep -qE '^\[T[0-9]+\]' || echo "⚠ start the message with the task id, e.g. \"[T3] ...\" — the master's review needs it"
if [ -n "${CHECKPOINT_ZONE:-}" ]; then
  OUT="$(echo "$CHANGED" | grep -v "^${CHECKPOINT_ZONE}" | grep -vE '^docs/(progress|screenshots)/' || true)"
  [ -n "$OUT" ] && echo "⚠ outside your zone ($CHECKPOINT_ZONE): $(echo "$OUT" | tr '\n' ' ')"
fi

# ---- record + commit + push ----
mkdir -p docs/progress
echo "- $TS · $BRANCH · $MSG" >> "docs/progress/$WHO.md"
git add "docs/progress/$WHO.md"
git commit -qm "checkpoint($WHO): $MSG" || echo "nothing new to commit"
if git push -q -u origin "$BRANCH" 2>/dev/null; then echo "✓ $TS  $WHO: $MSG  (pushed)"
else echo "⚠ $TS  committed locally, PUSH FAILED — check network, then: git push -u origin $BRANCH"; exit 1; fi
