#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

cd "$(dirname "$0")"

CODE_DIR=/opt/xui-sub
CONF_DIR=/etc/xui-sub
UNIT=/etc/systemd/system/xui-sub.service

install -d -m 755 "$CODE_DIR" "$CONF_DIR/templates"
install -m 755 app.py "$CODE_DIR/app.py"
install -m 644 templates/clash.yaml.j2 "$CONF_DIR/templates/clash.yaml.j2"
install -m 644 systemd/xui-sub.service "$UNIT"
install -m 644 README.md "$CODE_DIR/README.md"
install -m 644 config.example.toml "$CODE_DIR/config.example.toml"

systemctl daemon-reload

if [ ! -f "$CONF_DIR/config.toml" ]; then
  install -m 600 config.example.toml "$CONF_DIR/config.toml"
  echo "created $CONF_DIR/config.toml from example"
  echo "edit raw_path / upstream.host / branding.title, then re-run this script"
  exit 0
fi

/usr/bin/python3 "$CODE_DIR/app.py" -c "$CONF_DIR/config.toml" --check
systemctl enable xui-sub
systemctl restart xui-sub
systemctl --no-pager --lines=5 status xui-sub || true
