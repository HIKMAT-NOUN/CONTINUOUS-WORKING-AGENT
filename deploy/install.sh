#!/usr/bin/env bash
# Install myagent as a systemd service running as the current user.
# Usage: cd ~/myagent && ./deploy/install.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"

[ -f "$DIR/.env" ] || { echo "Create $DIR/.env first (cp .env.example .env)"; exit 1; }
chmod 600 "$DIR/.env"

if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv" || { echo "Install venv support: sudo apt install -y python3-venv"; exit 1; }
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

sed -e "s#__USER__#$USER_NAME#g" -e "s#__DIR__#$DIR#g" "$DIR/deploy/myagent.service" \
  | $SUDO tee /etc/systemd/system/myagent.service >/dev/null
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now myagent
$SUDO systemctl --no-pager status myagent | head -n 5
echo "Logs: journalctl -u myagent -f   or   tail -f $DIR/logs/agent.log"
