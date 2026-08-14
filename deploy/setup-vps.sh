#!/usr/bin/env bash
# earnfarm VPS 一键部署（Debian/Ubuntu，root 执行）。
# 用法：
#   EARNFARM_WEB_PASSWORD='你的访问密码' DOMAIN=earn.satloot.com bash setup-vps.sh
#
# 共存原则（本机可能已跑 faucet / Bitcoin Core 测试网等）：
# - earnfarm 只听 127.0.0.1:8777，systemd 单元名独占，绝不碰别的服务；
# - 反代自动探测：本机已有 nginx 占 80 → 挂进 nginx（不装 Caddy）；
#   已有 Caddy → 追加独立站点文件；两者皆无 → 装 Caddy。
#   任何情况下都不覆盖既有站点配置。
set -euo pipefail

DOMAIN="${DOMAIN:?请设置 DOMAIN，例如 DOMAIN=earn.satloot.com}"
WEB_PASSWORD="${EARNFARM_WEB_PASSWORD:?请设置 EARNFARM_WEB_PASSWORD（网页访问密码）}"
REPO="${REPO:-https://github.com/q3579338/earnfarm.git}"
APP_DIR=/opt/earnfarm
APP_PORT=8777

# 端口守卫：8777 被占说明本机已有东西在用，硬抢会打崩不知道什么服务
if ss -tln 2>/dev/null | grep -q ":${APP_PORT} "; then
    if ! systemctl is-active --quiet earnfarm 2>/dev/null; then
        echo "!! 端口 ${APP_PORT} 已被其他进程占用（且不是 earnfarm）。"
        echo "   请查明：ss -tlnp | grep ${APP_PORT}，或用 APP_PORT=其他端口 重跑。"
        exit 1
    fi
fi

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

# ---- 反代探测：谁在守 80 口，earnfarm 就挂进谁，绝不装第二个抢端口 ----
PROXY_KIND=none
if ss -tlnp 2>/dev/null | grep ':80 ' | grep -q nginx; then
    PROXY_KIND=nginx
elif command -v caddy >/dev/null; then
    PROXY_KIND=caddy
fi

if [ "$PROXY_KIND" = "none" ]; then
    echo "==> 本机 80 口无人值守，安装 Caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq && apt-get install -y -qq caddy
    PROXY_KIND=caddy
else
    echo "==> 检测到既有反代：${PROXY_KIND}，earnfarm 将挂进它"
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
ExecStart=${APP_DIR}/.venv/bin/python run.py --port ${APP_PORT}
Restart=on-failure
RestartSec=5
# 应用只听回环地址；公网入口只有 Caddy
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now earnfarm

echo "==> 配置反代（${PROXY_KIND}）"
# 橙云（Cloudflare 代理）模式：HTTPS 由边缘终结，源站只提供 HTTP。
# PROXIED=0 时（灰云直连）Caddy 会自动签 Let's Encrypt；nginx 分支只做 80，
# 灰云直连要 HTTPS 请自行配 certbot。
PROXIED="${PROXIED:-1}"

if [ "$PROXY_KIND" = "nginx" ]; then
    # 独立站点文件，绝不动 faucet 等既有 server 块
    NGX_CONF=/etc/nginx/conf.d/earnfarm.conf
    [ -d /etc/nginx/sites-enabled ] && NGX_CONF=/etc/nginx/sites-enabled/earnfarm.conf
    cat > "$NGX_CONF" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};
    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_http_version 1.1;
        # NiceGUI 靠 WebSocket 活着，这三行缺了页面会一直 Connection lost
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }
}
EOF
    nginx -t && systemctl reload nginx
else
    mkdir -p /etc/caddy/sites
    grep -q "^import sites/\*" /etc/caddy/Caddyfile 2>/dev/null \
        || printf '\nimport sites/*.caddy\n' >> /etc/caddy/Caddyfile
    if [ "$PROXIED" = "1" ]; then
        SITE="http://${DOMAIN}"
    else
        SITE="${DOMAIN}"
    fi
    cat > /etc/caddy/sites/earnfarm.caddy <<EOF
${SITE} {
    reverse_proxy 127.0.0.1:${APP_PORT}
}
EOF
    systemctl reload caddy || systemctl restart caddy
fi

echo
echo "完成。检查："
echo "  systemctl status earnfarm --no-pager | head -5"
echo "  curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: ${DOMAIN}' http://127.0.0.1/"
echo "  curl -sI https://${DOMAIN} | head -3"
