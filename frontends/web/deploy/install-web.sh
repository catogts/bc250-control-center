#!/usr/bin/env bash
# Install (or re-install) the BC250 web dashboard for the current user on a
# BC-250 that already has the bc250-control-center package installed.
#
#   bash frontends/web/deploy/install-web.sh [--rotate-token]
#
# What it does: installs server.py + static/ under ~/.local/share/bc250-web,
# creates ~/.config/bc250-web.env (0600) with a fresh token once, renders the
# exact-path sudoers rule for this account via visudo, installs the systemd
# user unit, enables it with linger, and prints the URL and token. It stores
# no password anywhere, and every privileged step is one visible sudo.
set -euo pipefail

WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
INSTALL_DIR="${BC250_WEB_DIR:-$HOME/.local/share/bc250-web}"
ENV_FILE="$HOME/.config/bc250-web.env"
UNIT_DIR="$HOME/.config/systemd/user"
HELPER_DIR=/usr/libexec/bc250-control-center
PORT="${BC250_WEB_PORT:-8088}"

fail() { echo "install-web: $*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v systemctl >/dev/null 2>&1 || fail "systemctl is required"
command -v bc250-control-center-cli >/dev/null 2>&1 || fail "bc250-control-center-cli not found; install the bc250-control-center package first"
for helper in bc250-fan-pwm-helper bc250-governor-config-helper bc250-cpu-smu-helper bc250-cu-helper; do
  [[ -x "$HELPER_DIR/$helper" ]] || fail "missing $HELPER_DIR/$helper (is the package installed?)"
done

install -d -m 0755 "$INSTALL_DIR/static"
install -m 0644 "$WEB_DIR/server.py" "$INSTALL_DIR/server.py"
install -m 0644 "$WEB_DIR/static/index.html" "$INSTALL_DIR/static/index.html"
python3 -m py_compile "$INSTALL_DIR/server.py"

if [[ ! -s "$ENV_FILE" || "${1:-}" == "--rotate-token" ]]; then
  umask 077
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  printf 'BC250_WEB_TOKEN=%s\n' "$TOKEN" > "$ENV_FILE"
else
  TOKEN="$(sed -n 's/^BC250_WEB_TOKEN=//p' "$ENV_FILE")"
fi
chmod 600 "$ENV_FILE"
[[ -n "$TOKEN" ]] || fail "empty token in $ENV_FILE"

install -d -m 0755 "$UNIT_DIR"
sed "s/@PORT@/$PORT/" "$WEB_DIR/deploy/bc250-web.service" > "$UNIT_DIR/bc250-web.service"

TMP_SUDOERS="$(mktemp)"
trap 'rm -f "$TMP_SUDOERS"' EXIT
sed "s/@USER@/$RUN_USER/" "$WEB_DIR/deploy/bc250-web.sudoers.template" > "$TMP_SUDOERS"
sudo visudo -cf "$TMP_SUDOERS"
sudo install -m 0440 -o root -g root "$TMP_SUDOERS" /etc/sudoers.d/bc250-web
rm -f "$TMP_SUDOERS"
trap - EXIT

systemctl --user daemon-reload
systemctl --user enable bc250-web
systemctl --user restart bc250-web
loginctl enable-linger "$RUN_USER" 2>/dev/null || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "BC250 web dashboard:  http://${IP:-127.0.0.1}:$PORT"
echo "token (paste in the page footer):  $TOKEN"
echo
echo "quick verification:"
echo "  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:$PORT/api/telemetry"
echo "  curl -s -H 'X-Auth: $TOKEN' http://127.0.0.1:$PORT/api/capabilities | head -c 300"
echo "  sudo -n $HELPER_DIR/bc250-fan-pwm-helper </dev/null   # READY / BYE"
