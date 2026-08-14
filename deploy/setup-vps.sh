#!/usr/bin/env bash
# earnfarm VPS 一键部署（Debian/Ubuntu，root 执行）。
# 用法：
#   EARNFARM_WEB_PASSWORD='你的访问密码' DOMAIN=earn.satloot.com bash setup-vps.sh
#
# 做的事：装 Python/git/Caddy → 拉代码 → venv 装依赖 → systemd 常驻 →
# Caddy 自动 HTTPS 反代。应用只听 127.0.0.1，公网入口只有 Caddy。
set -euo pipefail

DOMAIN="${DOMAIN:?请设置 DOMAIN，例如 DOMAIN=earn.satloot.com}"
WEB_PASSWORD="${EARNFARM_WEB_PASSWORD:?请设置 EARNFARM_WEB_PASSWORD（网页访问密码）}"
REPO="${REPO:-https://github.com/q3579338/earnfarm.git}"
APP_DIR=/opt/earnfarm

echo "==> 安装系统依赖"
apt-get update -qq
apt-get install -y -qq git curl debian-keyring debian-archive-keyring apt-transport-https

# Python >= 3.11（tomllib 需要）。系统自带的够新就直接用
PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq && apt-get install -y -qq python3.12 python3.12-venv
    PY=python3.12
else
    apt-get install -y -qq python3-venv
fi

echo "==> 安装 Caddy（自动 HTTPS）"
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy
fi

echo "==> 拉取代码"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
else
    git clone "$REPO" "$APP_DIR"
fi

echo "==> 虚拟环境与依赖"
[ -d "$APP_DIR/.venv" ] || $PY -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -U pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> systemd 服务"
# 密码放独立的 EnvironmentFile，权限 600——不进 unit 文件（systemctl show 会泄露）
install -m 600 /dev/null /etc/earnfarm.env
cat > /etc/earnfarm.env <<EOF
EARNFARM_WEB_PASSWORD=${WEB_PASSWORD}
EOF

cat > /etc/systemd/system/earnfarm.service <<EOF
[Unit]
Description=earnfarm web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=/etc/earnfarm.env
ExecStart=${APP_DIR}/.venv/bin/python run.py
Restart=on-failure
RestartSec=5
# 应用只听回环地址；公网入口只有 Caddy
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now earnfarm

echo "==> Caddy 反代 + 自动 HTTPS"
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8777
}
EOF
systemctl reload caddy

echo
echo "完成。检查："
echo "  systemctl status earnfarm --no-pager | head -5"
echo "  curl -sI https://${DOMAIN} | head -3"
echo
echo "前提：Cloudflare 里 ${DOMAIN} 的 A 记录已指向本机公网 IP，"
echo "且代理状态为「仅 DNS」（灰云）——Caddy 需要直连来签发证书。"
