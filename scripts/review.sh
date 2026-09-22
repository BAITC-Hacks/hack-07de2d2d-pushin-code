#!/usr/bin/env bash
# Headless review of a branch against the contract and its claimed task (master side).
# Usage: scripts/review.sh <branch> [T#]      REVIEWER=claude|codex (default: claude)
set -euo pipefail
BR="${1:?branch}"; TASK="${2:-}"; REVIEWER="${REVIEWER:-claude}"
git fetch -q origin "$BR"
SHA="$(git rev-parse --short "origin/$BR")"
BASE="$(git merge-base origin/main "origin/$BR")"
[ -z "$TASK" ] && TASK="$(git log "$BASE..origin/$BR" --format=%s | grep -oE '\[T[0-9]+\]' | head -1 | tr -d '[]' || true)"
FILES="$(git diff --name-only "$BASE" "origin/$BR")"
DIFF="$(git diff "$BASE" "origin/$BR" -- . ':!docs/progress' ':!docs/review' | head -c 60000)"
mkdir -p docs/review; OUT="docs/review/${BR//\//-}-$SHA.md"
PROMPT="$(sed -e "s|{{BRANCH}}|$BR|g" -e "s|{{TASK}}|${TASK:-не указана}|g" -e "s|{{SHA}}|$SHA|g" scripts/review-prompt.md)

## Изменённые файлы
$FILES

## Дифф (обрезан до 60 КБ)
\`\`\`diff
$DIFF
\`\`\`"
case "$REVIEWER" in
  claude) claude -p "$PROMPT" --allowedTools "Read,Grep,Glob" > "$OUT" ;;
  codex)  codex exec "$PROMPT" > "$OUT" ;;
  dry)    printf "%s\n" "$PROMPT" > "$OUT" ;;
  *) echo "REVIEWER must be claude or codex"; exit 1 ;;
esac
echo "→ $OUT"; cat "$OUT"
