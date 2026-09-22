#!/usr/bin/env bash
# Run ON the server from the repo root:  infra/deploy.sh
# From a laptop:  ssh $DEPLOY_HOST 'cd /srv/hackalem && infra/deploy.sh'
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; . ./.env; set +a
: "${DOMAIN:?DOMAIN missing in .env}"

# When invoked from the bare repo's post-receive hook the working tree is already
# at the pushed commit — pulling would fail (no upstream configured).
if [ "${DEPLOY_FROM_HOOK:-0}" != "1" ]; then
  git pull --ff-only
fi
docker compose -f infra/docker-compose.yml --env-file .env up -d --build --remove-orphans

for i in $(seq 1 40); do
  if curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1; then
    echo "✓ live: https://$DOMAIN  ($(TZ=Asia/Almaty date '+%H:%M') Almaty)"; exit 0
  fi
  sleep 3
done
echo "✗ health check failed after 120s — last logs:"
docker compose -f infra/docker-compose.yml logs --tail=50
exit 1
