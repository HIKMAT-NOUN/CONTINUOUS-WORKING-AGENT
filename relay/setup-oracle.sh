#!/usr/bin/env bash
# One-shot relay setup for an Oracle (or any) Linux VM, exposed via Cloudflare Tunnel.
# Usage:  bash setup-oracle.sh agent.yourdomain.com
#
# It installs the relay + cloudflared, runs both as services, and prints the exact values
# to put in the app and in the agent's .env. Safe to re-run: it reuses the same secrets.
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  read -rp "Hostname for the agent (e.g. agent.yourdomain.com): " DOMAIN
fi
[ -n "$DOMAIN" ] || { echo "no hostname given"; exit 1; }

REPO="https://github.com/HIKMAT-NOUN/continuous-working-agent"
HOME_DIR="$HOME"
USER_NAME="$(id -un)"

echo "==> Installing base packages"
if command -v apt >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y git python3 python3-venv curl
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y git python3 curl
else
  echo "no apt or dnf found — unsupported distro"; exit 1
fi

echo "==> Fetching the relay code"
if [ -d "$HOME_DIR/agent-relay/.git" ]; then
  git -C "$HOME_DIR/agent-relay" pull --ff-only || true
else
  git clone "$REPO" "$HOME_DIR/agent-relay"
fi

echo "==> Python environment"
python3 -m venv "$HOME_DIR/relay-venv"
"$HOME_DIR/relay-venv/bin/pip" install -q --upgrade pip
"$HOME_DIR/relay-venv/bin/pip" install -q -r "$HOME_DIR/agent-relay/relay/requirements.txt"

echo "==> Secrets"
KEYS="$HOME_DIR/.myagent-relay-keys"
if [ -f "$KEYS" ]; then
  # shellcheck disable=SC1090
  source "$KEYS"
  echo "    (reusing existing keys from $KEYS)"
else
  AGENT_KEY="$(openssl rand -hex 24)"
  USER_TOKEN="$(openssl rand -hex 24)"
  printf 'AGENT_KEY=%s\nUSER_TOKEN=%s\n' "$AGENT_KEY" "$USER_TOKEN" > "$KEYS"
  chmod 600 "$KEYS"
fi

echo "==> Relay service (listens on localhost only)"
sudo tee /etc/systemd/system/myagent-relay.service >/dev/null <<EOF
[Unit]
Description=myagent relay
After=network-online.target
Wants=network-online.target
[Service]
User=$USER_NAME
ExecStart=$HOME_DIR/relay-venv/bin/python $HOME_DIR/agent-relay/relay/relay.py
Restart=always
RestartSec=3
Environment=RELAY_HOST=127.0.0.1
Environment=RELAY_PORT=8900
Environment=RELAY_AGENT_KEY=$AGENT_KEY
Environment=RELAY_USER_TOKEN=$USER_TOKEN
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now myagent-relay
sleep 1
echo -n "    local health: "; curl -s localhost:8900/healthz || true; echo

echo "==> Installing cloudflared"
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) A=amd64 ;;
    aarch64|arm64) A=arm64 ;;
    *) echo "unknown CPU arch $ARCH"; exit 1 ;;
  esac
  curl -L -o /tmp/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$A"
  sudo install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared
fi
CFBIN="$(command -v cloudflared)"

echo "==> Cloudflare login (opens a link — approve it in your browser, pick your domain)"
if [ ! -f "$HOME_DIR/.cloudflared/cert.pem" ]; then
  "$CFBIN" tunnel login
fi

echo "==> Tunnel + DNS route"
if ! "$CFBIN" tunnel list 2>/dev/null | grep -q "myagent-relay"; then
  "$CFBIN" tunnel create myagent-relay
fi
"$CFBIN" tunnel route dns myagent-relay "$DOMAIN" || true
TUNNEL_ID="$("$CFBIN" tunnel list | awk '/myagent-relay/{print $1; exit}')"

mkdir -p "$HOME_DIR/.cloudflared"
cat > "$HOME_DIR/.cloudflared/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $HOME_DIR/.cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: $DOMAIN
    service: http://localhost:8900
  - service: http_status:404
EOF

echo "==> cloudflared service"
sudo tee /etc/systemd/system/cloudflared.service >/dev/null <<EOF
[Unit]
Description=cloudflared tunnel
After=network-online.target
Wants=network-online.target
[Service]
User=$USER_NAME
ExecStart=$CFBIN --no-autoupdate --config $HOME_DIR/.cloudflared/config.yml tunnel run
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
sleep 4

echo
echo "=================  DONE  ================="
echo -n "Public health check: "; curl -s "https://$DOMAIN/healthz" || echo "(not reachable yet — give DNS a minute, then: curl https://$DOMAIN/healthz)"
echo
echo
echo "PUT THESE IN THE ANDROID APP:"
echo "  Relay URL:  wss://$DOMAIN/ws/app"
echo "  User token: $USER_TOKEN"
echo
echo "PUT THESE IN THE AGENT'S .env ON KALI (then: sudo systemctl restart myagent):"
echo "  RELAY_URL=wss://$DOMAIN/ws/agent"
echo "  RELAY_AGENT_KEY=$AGENT_KEY"
echo "========================================="
