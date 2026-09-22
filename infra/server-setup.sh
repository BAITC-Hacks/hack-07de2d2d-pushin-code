#!/usr/bin/env bash
# One-time server preparation for the deploy host. Idempotent — safe to re-run.
# Run ON the server:  bash server-setup.sh
# Or from a laptop:   ssh <user>@<host> 'bash -s' < infra/server-setup.sh
#
# Installs Docker CE + compose plugin, opens 80/443, and sets up a bare repo at
# /srv/hackalem.git with a post-receive hook that checks out into /srv/hackalem
# and runs infra/deploy.sh. Pushing straight to the server means the deploy path
# does not depend on GitHub access from the venue.
set -euo pipefail

APP_DIR=/srv/hackalem
BARE_DIR=/srv/hackalem.git

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

say "1/6 · Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "already installed: $(docker --version)"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo apt-get -qq update
  sudo apt-get -qq install -y ca-certificates curl gnupg
  if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get -qq update
  sudo apt-get -qq install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

TARGET_USER="${SUDO_USER:-$USER}"
say "2/6 · docker group for $TARGET_USER"
sudo usermod -aG docker "$TARGET_USER"
echo "re-login required for the group to take effect in new shells"

say "3/6 · firewall"
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 22/tcp  >/dev/null 2>&1 || true
  sudo ufw allow 80/tcp  >/dev/null 2>&1 || true
  sudo ufw allow 443/tcp >/dev/null 2>&1 || true
  sudo ufw status | head -8
else
  echo "ufw not installed — check the provider's firewall for 80/443"
fi

say "4/6 · directories"
sudo mkdir -p "$APP_DIR" "$BARE_DIR"
sudo chown -R "$TARGET_USER:$TARGET_USER" "$APP_DIR" "$BARE_DIR"
[ -d "$BARE_DIR/objects" ] || git init --bare -q "$BARE_DIR"
[ -d "$APP_DIR/.git" ] || git -C "$APP_DIR" init -q

say "5/6 · post-receive hook"
cat > "$BARE_DIR/hooks/post-receive" <<HOOK
#!/usr/bin/env bash
set -euo pipefail
APP_DIR=$APP_DIR
while read -r _old _new ref; do
  [ "\$ref" = "refs/heads/main" ] || continue
  git --work-tree="\$APP_DIR" --git-dir="$BARE_DIR" checkout -f main
  cd "\$APP_DIR"
  if [ -f .env ]; then
    echo "--- deploying ---"
    DEPLOY_FROM_HOOK=1 infra/deploy.sh || echo "deploy failed — see logs above"
  else
    echo "no .env in \$APP_DIR — create it from infra/.env.example, then push again"
  fi
done
HOOK
chmod +x "$BARE_DIR/hooks/post-receive"

say "6/6 · next steps"
cat <<TXT
On your laptop, inside the repository clone:

  git remote add deploy $TARGET_USER@\$(hostname -I | awk '{print \$1}'):$BARE_DIR
  git push deploy main

Before the first deploy, create $APP_DIR/.env (see infra/.env.example):

  DOMAIN=pushin.codes
  DEPLOY_COMPOSE_FILE=infra/docker-compose.hosted.yml   # host already serves 80/443
  OPENAI_API_KEY=sk-...

Then any 'git push deploy main' checks out and redeploys.
TXT
