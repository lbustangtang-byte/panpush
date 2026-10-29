#!/usr/bin/env bash
# 发布环境入口：先确保 PanSou 在线，再用注入的 $PORT 启动工作台。
#
# 与 start.sh 的区别：
#   - 端口来自平台注入的 PORT，而不是固定 5003
#   - 从这里注入 WEB_PASSWORD，让对外链接必须登录；
#     本地 5003 那份跑的是 .env 里的空密码，只对本机放行，方便预览
set -e
cd "$(dirname "$0")"
mkdir -p data/logs

# 1) PanSou 只监听 127.0.0.1:8888，不对外暴露；不在就拉起来
if ! curl -s -m 3 http://127.0.0.1:8888/api/health >/dev/null 2>&1; then
  echo "[发布] PanSou 未就绪，尝试启动"
  PY_BOOT=".venv/bin/python"
  [ -x "$PY_BOOT" ] || PY_BOOT="python3"
  "$PY_BOOT" main.py pansou start --wait 60 > data/logs/pansou.log 2>&1 || \
    echo "[发布] PanSou 启动失败，搜索与预览将不可用（页面仍可打开）"
fi

# 2) 对外链接必须带密码，否则等于把网盘凭证挂在公网
if [ -f data/.web_password ]; then
  WEB_PASSWORD="$(cat data/.web_password)"
  export WEB_PASSWORD
  export WEB_USER="${WEB_USER:-admin}"
else
  echo "[发布] 缺少 data/.web_password，拒绝以无密码方式对外启动"
  exit 1
fi

# 3) 启动工作台（serve_web.py 自己读 PORT）
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
# 本地开发用的 5003 常驻，这里默认换 8080 避免撞端口；
# 平台注入 PORT 时以注入值为准。
if [ -z "${PORT:-}" ] || [ "${PORT}" = "5003" ]; then
  PORT=8080
fi
export PORT

echo "[发布] 工作台监听 0.0.0.0:${PORT}，已启用密码登录"
exec "$PY" serve_web.py
